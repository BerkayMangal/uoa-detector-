"""Original signal reasons remain causal, finite, read-only and historical."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from webapp import signal_explanation as explain
from webapp.board.db import make_engine, session_factory
from webapp.live_alpha import store
from webapp.opening import policy, storage
from webapp.opening.model import Advice, clock
from webapp.opening.view import Statement

from uoa_detector.live_alpha.settings import load_live_alpha_settings

T = datetime(2026, 11, 4, 14, 45, tzinfo=UTC)
BASE = load_live_alpha_settings()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    result = make_engine(f"sqlite:///{tmp_path / 'explanation.db'}")
    store.ensure_live_tables(result)
    storage.ensure(result)
    return result


def source(*, at: datetime = T, identity: str = "original", path: str = "P1_news_continuation", direction: str = "up",
           price_changes: dict[str, object] | None = None, flow_changes: dict[str, object] | None = None,
           news_changes: dict[str, object] | None = None, context: bool = True) -> store.ScanRow:
    card = dict(ticker="NVDA", direction=direction, recommendation="BUY", readiness="READY", market_mode="LIVE",
        opportunity_id=f"{at.date()}:NVDA:{direction}", decision_as_of=at.isoformat(), path=path,
        plain_reason="Akış ve fiyat teyidi; özgün açıklama.", policy_version=BASE.policy_version, profile_sha256=BASE.profile_sha256,
        instrument={"preferred": "stock"}, stock_plan=dict(entry_ref=100, entry_ref_at=at.isoformat(), stop=97, target=106,
        shares=20, planned_risk_usd=60), options=[], option_evidence="$600k yukarı akış; özgün metin.",
        price_evidence="SPY-relative +0.8pp; bu metni sayıya çevirme.", news_evidence="Özgün UW başlık metni.",
        counter_argument="Akış hedge olabilir.", invalidation="Stop altı.")
    prices: dict[str, object] = dict(ticker="NVDA", spot=100, spot_fetched_at=at.isoformat(), prev_close=99, atr=2,
                                   benchmark_move=-0.005, state="checked_found", source_session=at.date().isoformat(),
                                   prev_close_day=(at-timedelta(days=1)).date().isoformat())
    prices.update(price_changes or {})
    flow: dict[str, object] = dict(ticker="NVDA", last_ts=at.isoformat(), premium_up=600000, premium_down=100000,
                                 premium_side_unknown=25000, prints_deduped=9, distinct_contracts=3)
    flow.update(flow_changes or {})
    news: dict[str, object] = dict(state="checked_found", checked_at=at.isoformat())
    news.update(news_changes or {})
    snapshot = dict(policy_version=BASE.policy_version, profile_sha256=BASE.profile_sha256, cards=[card], flow_current=True,
        price_observations={"NVDA": dict(spot=100, source_at=at.isoformat(), session=at.date().isoformat())},
        flow_observations={"NVDA": flow}, news_observations={"NVDA": news})
    if context:
        snapshot["news_research_context"] = dict(version="physical_news_v1", prices={"NVDA": prices})
    return store.ScanRow(identity, at, at+timedelta(seconds=2), "LIVE", "ok", "run", snapshot)


def publish(engine: Engine, scan: store.ScanRow | None = None, *, action: str | None = None) -> Statement:
    scan = scan or source()
    at = scan.finished_at+timedelta(seconds=1)
    advice = policy.project(scan, scan.snapshot["cards"][0], at, clock(at), set())
    if action:
        advice = advice.model_copy(update={"action": action})
    store.write_scan(engine, scan, BASE.policy_version)
    storage.publish(engine, scan, at, clock(at), (advice,))
    with session_factory(engine)() as session:
        row = session.scalar(select(storage.OpeningDecision).where(storage.OpeningDecision.published_at == at))
        assert row is not None
        return Statement(row.key, advice, at, scan.scan_id, True)


def test_original_numeric_evidence_and_distinct_clocks(engine: Engine) -> None:
    statement = publish(engine)
    result = explain.build(engine, T+timedelta(minutes=1), statements=(statement,))[statement.key]
    assert result.source_valid and result.current
    assert result.first_issued_at == statement.at == result.statement_at
    assert result.scan_started_at == result.decision_at == T
    assert result.scan_finished_at == T+timedelta(seconds=2)
    assert result.expires_at == statement.advice.expires_at
    assert result.reasons == statement.advice.reasons and result.evidence == statement.advice.evidence
    assert result.price.spot == 100
    assert result.price.move_pct == pytest.approx((100/99-1)*100)
    assert result.price.move_atr == 0.5
    assert result.price.relative_pp == pytest.approx((100/99-1)*100+0.5)
    assert result.flow.directional_premium_usd == 600000
    assert result.flow.opposite_premium_usd == 100000 and result.flow.unknown_premium_usd == 25000
    assert result.flow.dominant_share_pct == pytest.approx(600000/700000*100)
    assert result.flow.prints == 9 and result.flow.contracts == 3
    assert result.news.qualifying_vote is True and result.support.role == "uw_news_selected"


def test_later_scan_and_new_headline_cannot_rewrite_original(engine: Engine) -> None:
    statement = publish(engine)
    first = explain.build(engine, T+timedelta(seconds=4), keys=(statement.key,))[statement.key]
    later = source(at=T+timedelta(minutes=2), identity="later", path="P2_market_relative_flow", news_changes={"state": "checked_none"})
    later.snapshot["cards"][0]["plain_reason"] = "Yeni taramanın farklı açıklaması."
    publish(engine, later)
    second = explain.build(engine, T+timedelta(minutes=3), keys=(statement.key,))[statement.key]
    assert first == second
    assert "Yeni taramanın" not in str(second)


def test_first_buy_and_update_publication_are_separate(engine: Engine) -> None:
    first = publish(engine)
    update_statement = publish(engine, source(at=T+timedelta(minutes=1), identity="update"))
    result = explain.build(engine, T+timedelta(minutes=2), keys=(first.key, update_statement.key))
    assert result[update_statement.key].first_issued_at == first.at
    assert result[update_statement.key].statement_at == update_statement.at
    assert not result[update_statement.key].current


def test_future_first_buy_is_not_attached_to_earlier_wait(engine: Engine) -> None:
    wait = publish(engine, action="WAIT")
    publish(engine, source(at=T+timedelta(minutes=1), identity="first-buy"))
    result = explain.build(engine, T+timedelta(minutes=2), keys=(wait.key,))[wait.key]
    assert result.first_issued_at is None


def test_corrupt_first_source_does_not_supply_first_clock_to_valid_update(engine: Engine) -> None:
    first = publish(engine)
    later = publish(engine, source(at=T+timedelta(minutes=1), identity="valid-update"))
    with engine.begin() as connection:
        connection.execute(update(store.LiveScan).where(store.LiveScan.scan_id == first.scan_id).values(snapshot_json="{}"))
    result = explain.build(engine, T+timedelta(minutes=2), keys=(later.key,))[later.key]
    assert result.source_valid and result.first_issued_at is None


@pytest.mark.parametrize("change", [{"state": "failed"}, {"state": "checked_none"}, {"source_session": "2026-11-05"},
    {"prev_close_day": "2026-11-04"}, {"prev_close_day": None}])
def test_unverified_price_quality_keeps_only_recorded_spot(engine: Engine, change: dict[str, object]) -> None:
    statement = publish(engine, source(price_changes=change))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.source_valid and result.price.spot == 100 and result.price.status == "partial"
    assert result.price.move_pct is None and result.price.move_atr is None and result.price.relative_pp is None


@pytest.mark.parametrize("damage", ["snapshot", "card", "profile", "cycle_scan", "cycle_clock", "decision_clock"])
def test_provenance_failure_keeps_saved_reasons_but_no_metrics(engine: Engine, damage: str) -> None:
    statement = publish(engine)
    with engine.begin() as connection:
        if damage in {"snapshot", "card", "profile", "decision_clock"}:
            with session_factory(engine)() as session:
                scan = session.get(store.LiveScan, statement.scan_id)
                assert scan is not None
                raw = json.loads(scan.snapshot_json)
            if damage == "snapshot":
                raw["new_headline"] = "Later headline"
            elif damage == "card":
                raw["cards"][0]["plain_reason"] = "Overwritten"
            elif damage == "profile":
                raw["profile_sha256"] = "f"*64
            else:
                raw["cards"][0]["decision_as_of"] = (T+timedelta(minutes=1)).isoformat()
            connection.execute(update(store.LiveScan).values(snapshot_json=json.dumps(raw)))
        elif damage == "cycle_scan":
            connection.execute(update(storage.OpeningCycle).values(scan_id="other-scan"))
        else:
            connection.execute(update(storage.OpeningCycle).values(published_at=T+timedelta(minutes=1)))
    result = explain.build(engine, T+timedelta(minutes=2), statements=(statement,))[statement.key]
    assert not result.source_valid and not result.current
    assert result.reasons == statement.advice.reasons and result.evidence == statement.advice.evidence
    assert result.price.spot is None and result.flow.directional_premium_usd is None
    assert result.news.qualifying_vote is None and result.decision_at is None


def test_future_original_scan_is_not_used(engine: Engine) -> None:
    scan = source()
    statement = publish(engine, scan)
    with engine.begin() as connection:
        connection.execute(update(store.LiveScan).values(finished_at=T+timedelta(days=1)))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert not result.source_valid and result.scan_finished_at is None


@pytest.mark.parametrize("clock_value", ["2026-11-04T14:45:00", "2026-11-04T14:45:01Z", "2026-11-05T14:00:00Z", "bad"])
def test_naive_future_and_post_decision_input_clocks_unknown(engine: Engine, clock_value: str) -> None:
    statement = publish(engine, source(price_changes={"spot_fetched_at": clock_value}, flow_changes={"last_ts": clock_value},
                                       news_changes={"checked_at": clock_value}))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.source_valid
    assert result.price.status == result.flow.status == "unknown"
    assert result.news.state == "unknown" and result.news.qualifying_vote is None


@pytest.mark.parametrize("value", ["600000", True, -1, None])
def test_non_numeric_or_invalid_flow_does_not_get_parsed(engine: Engine, value: object) -> None:
    statement = publish(engine, source(flow_changes={"premium_up": value}))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.flow.status == "unknown" and result.flow.directional_premium_usd is None


def test_foreign_flow_body_is_not_attributed_to_ticker_key(engine: Engine) -> None:
    statement = publish(engine, source(flow_changes={"ticker": "AAPL"}))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.source_valid and result.flow.directional_premium_usd is None


def test_nonfinite_aggregate_and_price_fields_fail_closed(engine: Engine) -> None:
    statement = publish(engine, source(flow_changes={"premium_up": 1e308, "premium_down": 1e308}, price_changes={"prev_close": "99.0"}))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.source_valid and result.flow.directional_premium_usd is None
    assert result.price.move_pct is None and result.price.move_atr is None


def test_missing_context_preserves_raw_evidence_without_prose_numeric_parsing(engine: Engine) -> None:
    statement = publish(engine, source(context=False))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.source_valid and result.price.spot == 100
    assert result.price.move_atr is None and result.price.relative_pp is None
    assert result.evidence == statement.advice.evidence


@pytest.mark.parametrize("path,news_state,direction,role,vote", [
    ("P1_news_continuation", "checked_found", "up", "uw_news_selected", True),
    ("P2_market_relative_flow", "checked_found", "up", "relative_strength_selected", None),
    ("P3_pullback_entry", "checked_found", "up", "pullback", None),
    ("P4_bearish_setup", "checked_found", "down", "bearish", None),
    ("unknown", "checked_found", "up", "unknown", None),
])
def test_saved_support_attribution_is_not_news_state_inference(engine: Engine, path: str, news_state: str, direction: str,
                                                              role: str, vote: bool | None) -> None:
    statement = publish(engine, source(path=path, direction=direction, news_changes={"state": news_state}))
    result = explain.build(engine, T+timedelta(minutes=1), keys=(statement.key,))[statement.key]
    assert result.support.role == role and result.news.qualifying_vote is vote
    if role == "uw_news_selected":
        assert "başarısız olduğu anlamına gelmez" in result.support.text
    if role == "bearish":
        assert "uygulanmaz" in result.support.text
    assert "AL oyu vermez" in result.curated_role


def test_current_is_inherited_only_from_same_saved_statement(engine: Engine) -> None:
    statement = publish(engine)
    historical = replace(statement, current=False)
    assert not explain.build(engine, T+timedelta(seconds=4), statements=(historical,))[statement.key].current
    wrong = replace(statement, scan_id="newest-unrelated")
    result = explain.build(engine, T+timedelta(seconds=4), statements=(wrong,))[statement.key]
    assert not result.current and not result.source_valid


def test_supplied_future_statement_does_not_enter_past_projection(engine: Engine) -> None:
    statement = publish(engine)
    assert explain.build(engine, T, statements=(statement,), keys=(statement.key,)) == {}


def test_projection_is_sql_read_only_and_never_fetches(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    statement = publish(engine)
    sql: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, query, parameters, context, executemany: sql.append(query))
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("presentation must not fetch news")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    result = explain.build(engine, T+timedelta(seconds=4), keys=(statement.key,))
    assert result[statement.key].source_valid
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for s in sql)


def test_missing_storage_falls_back_without_creating_tables(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'absent.db'}")
    advice = Advice(ticker="NVDA", action="WAIT", headline="Bekle", reasons=("Kayıtlı neden.",))
    statement = Statement("old", advice, T, "missing", True)
    result = explain.build(engine, T+timedelta(minutes=1), statements=(statement,))["old"]
    assert result.reasons == advice.reasons and not result.current
    assert not result.source_valid and result.first_issued_at is None


@pytest.mark.parametrize("value", [None, "bad", "2026-11-04T14:45:00", datetime(2026, 11, 4, 14, 45)])
def test_unknown_london_clock_never_assumes_timezone(value: datetime | str | None) -> None:
    assert explain.london_time(value) == "Bilinmiyor"


def test_london_date_and_dst_are_explicit() -> None:
    assert explain.london_time(T) == "04.11.2026 14:45:00 GMT · Europe/London"
    assert explain.london_time("2026-07-01T14:45:00Z") == "01.07.2026 15:45:00 BST · Europe/London"
