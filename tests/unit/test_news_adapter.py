"""Strict feature exports never reconstruct old text or replace unknown first rows."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table, event
from sqlalchemy.engine import Engine
from webapp.alpha_lab.db import Price as StoredPrice
from webapp.alpha_lab.db import append as append_rows
from webapp.board.db import make_engine
from webapp.live_alpha import store
from webapp.news_research.adapter import audit, export
from webapp.news_research.config import profile_hash
from webapp.news_research.experiment import roster
from webapp.news_research.report import build
from webapp.news_research.storage import append, ensure

from uoa_detector.live_alpha.settings import load_live_alpha_settings

NOW = datetime(2026, 11, 4, 15, tzinfo=UTC)
LIVE = load_live_alpha_settings()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    value = make_engine(f"sqlite:///{tmp_path / 'news-adapter.db'}")
    store.ensure_live_tables(value)
    ensure(value)
    cast(Table, StoredPrice.__table__).create(value, checkfirst=True)
    return value


def snapshot(at: datetime = NOW) -> dict[str, object]:
    def price(spot: float, prior: float, atr: float) -> dict[str, object]:
        return dict(spot=spot, spot_fetched_at=(at - timedelta(seconds=15)).isoformat(),
                    prev_close=prior, prev_close_day="2026-11-03", atr=atr,
                    benchmark_move=.001, source_session="2026-11-04", state="checked_found")
    return dict(
        policy_version=LIVE.policy_version, profile_sha256=LIVE.profile_sha256,
        flow_current=True,
        flow_observations={"NVDA": dict(premium_up=350000, premium_down=50000,
            premium_side_unknown=0, distinct_contracts=3, last_ts=(at - timedelta(seconds=60)).isoformat())},
        news_observations={"NVDA": dict(state="checked_found", checked_at=(at - timedelta(seconds=30)).isoformat())},
        news_research_context=dict(version="physical_news_v1", profile_hash=profile_hash(),
            prices={"NVDA": price(101., 100., 2.), "SPY": price(500.5, 500., 3.)}),
    )


def scan(engine: Engine, key: str = "first", at: datetime = NOW,
         data: dict[str, object] | None = None) -> None:
    store.write_scan(engine, store.ScanRow(key, at-timedelta(seconds=5), at,
        "LIVE", "ok", "live", data or snapshot(at)), LIVE.policy_version)


def news(engine: Engine) -> None:
    append(engine, "NVDA", {"data": [{"headline": "Company wins contract", "source": "Wire",
        "tickers": ["NVDA"], "created_at": (NOW-timedelta(hours=1)).isoformat()}]},
        NOW-timedelta(seconds=30), NOW-timedelta(seconds=20),
        clock=lambda: NOW-timedelta(seconds=10), application_checked_at=NOW-timedelta(seconds=30))


def point(engine: Engine, key: str, at: datetime, known: datetime, value: float = 100) -> None:
    append_rows(engine, cast(Table, StoredPrice.__table__), [dict(key=key,ticker="NVDA",
        source_at=at,observed_at=known,value=value,session_day="2026-11-04")])


def test_export_keeps_news_and_relative_gates_independent_of_source_watch(engine: Engine) -> None:
    data = snapshot()
    data["cards"] = [{"ticker": "NVDA", "recommendation": "WATCH"}]
    scan(engine, data=data)
    news(engine)
    result = export(engine, NOW)
    assert len(result.opportunities) == 1
    observed = result.opportunities[0]
    assert observed.at == NOW and observed.base is True
    assert observed.rs is True and observed.news is True
    assert result.sessions == (NOW.date(),)


def test_legacy_first_row_cannot_be_replaced_by_later_complete_row(engine: Engine) -> None:
    old = snapshot(NOW-timedelta(minutes=5))
    old.pop("news_research_context")
    scan(engine, "legacy", NOW-timedelta(minutes=5), old)
    scan(engine)
    news(engine)
    result = audit(engine, NOW)
    assert result.dataset is not None
    frozen = roster(result.dataset)
    assert len(frozen) == 1 and frozen[0].key == "legacy:NVDA" and frozen[0].base is None
    assert result.legacy_scans == 1 and result.new_context_scans == 1


@pytest.mark.parametrize("damage", ["session", "prior", "future", "profile", "news_failure", "benchmark"])
def test_unknown_source_never_becomes_a_positive_gate(engine: Engine, damage: str) -> None:
    data = snapshot()
    context = cast(dict[str, object], data["news_research_context"])
    prices = cast(dict[str, dict[str, object]], context["prices"])
    if damage == "session":
        prices["NVDA"]["source_session"] = "2026-11-03"
    elif damage == "prior":
        prices["NVDA"]["prev_close_day"] = "2026-11-02"
    elif damage == "future":
        prices["NVDA"]["spot_fetched_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif damage == "profile":
        context["profile_hash"] = "wrong"
    elif damage == "news_failure":
        data["news_observations"] = {"NVDA": dict(state="failed", checked_at=NOW.isoformat())}
    else:
        prices["SPY"]["source_session"] = "2026-11-03"
    scan(engine, data=data)
    news(engine)
    item = export(engine, NOW).opportunities[0]
    if damage in ("session", "prior", "future", "profile"):
        assert item.base is None
    elif damage == "news_failure":
        assert item.base is True and item.news is None
    else:
        assert item.base is True and item.rs is None


def test_old_and_preassessment_scans_do_not_create_a_later_asof_roster(engine: Engine) -> None:
    scan(engine, "premature", datetime(2026,11,4,14,40,tzinfo=UTC))
    scan(engine, "future", NOW+timedelta(seconds=1))
    result = export(engine, NOW)
    assert result.opportunities == () and result.sessions == ()


def mark_scan(engine: Engine, key: str, source_at: datetime, finished_at: datetime, value: float = 100.) -> None:
    data = snapshot(finished_at)
    data["flow_observations"] = {}
    context = cast(dict[str, object], data["news_research_context"])
    context["prices"] = {"NVDA": dict(spot=value, spot_fetched_at=source_at.isoformat(),
                                      source_session="2026-11-04")}
    scan(engine, key, finished_at, data)


def test_price_duplicate_keeps_earliest_completed_scan_and_future_revision_cannot_rewrite_it(engine: Engine) -> None:
    mark_scan(engine, "a", NOW-timedelta(minutes=1), NOW-timedelta(seconds=30))
    mark_scan(engine, "b", NOW-timedelta(minutes=1), NOW-timedelta(seconds=20))
    mark_scan(engine, "future", NOW-timedelta(minutes=1), NOW+timedelta(seconds=1), value=200.)
    result = export(engine, NOW)
    assert len(result.prices) == 1 and result.prices[0].known_at == NOW-timedelta(seconds=30)
    with pytest.raises(ValueError, match="conflicting"):
        export(engine, NOW+timedelta(seconds=2))


def test_truncated_export_fails_instead_of_changing_the_roster(engine: Engine) -> None:
    scan(engine, "a", NOW-timedelta(seconds=1))
    scan(engine, "b")
    with pytest.raises(ValueError, match="truncated"):
        export(engine, NOW, limit=1)


def test_report_is_read_only_and_does_not_run_the_experiment(engine: Engine) -> None:
    scan(engine)
    news(engine)
    writes: list[str] = []
    def executed(_c: object,_cur: object,sql: str,_params: object,_ctx: object,_many: bool) -> None:
        if sql.lstrip().upper().startswith(("INSERT","UPDATE","DELETE","CREATE")):
            writes.append(sql)
    event.listen(engine, "before_cursor_execute", executed)
    result = build(engine, NOW)
    assert result.status == "COLLECTING" and result.new_context_scans == 1
    assert result.packet_count == 1 and result.receipt_count == 1
    assert result.causal_opportunities is None and result.inference == "NOT_PERFORMED"
    assert writes == [] and result.winner is None


def test_missing_tables_remain_unknown(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    result = build(engine, NOW)
    assert result.status == "STORAGE_UNAVAILABLE" and result.packet_count is None
    assert result.total_scans is None
    with pytest.raises(ValueError, match="unavailable"):
        export(engine, NOW)


def test_isolated_low_flow_scan_does_not_manufacture_a_complete_idle_session(engine: Engine) -> None:
    data = snapshot()
    flow = cast(dict[str, dict[str, object]], data["flow_observations"])
    flow["NVDA"]["premium_up"] = 10.
    scan(engine, data=data)
    result = export(engine, NOW)
    assert result.opportunities[0].base is False and result.sessions == ()


def test_export_identity_binds_the_actual_news_packet_even_when_the_gate_is_unchanged(engine: Engine) -> None:
    scan(engine)
    news(engine)
    first = audit(engine, NOW)
    append(engine, "NVDA", {"data": [{"headline": "Company wins a second contract", "source": "Wire",
        "tickers": ["NVDA"], "created_at": (NOW-timedelta(hours=1)).isoformat()}]},
        NOW-timedelta(seconds=8), NOW-timedelta(seconds=6), clock=lambda: NOW-timedelta(seconds=4), application_checked_at=NOW-timedelta(seconds=30))
    second = audit(engine, NOW)
    assert first.dataset is not None and second.dataset is not None
    assert first.dataset.opportunities[0].news is True and second.dataset.opportunities[0].news is True
    assert first.dataset.identity != second.dataset.identity
    assert first.news_provenance[0].packet_key != second.news_provenance[0].packet_key


def test_new_application_check_cannot_reuse_an_old_physically_fresh_packet(engine: Engine) -> None:
    news(engine)
    data = snapshot()
    data["news_observations"] = {"NVDA": dict(state="checked_found", checked_at=(NOW-timedelta(seconds=5)).isoformat())}
    scan(engine, data=data)
    item = export(engine, NOW).opportunities[0]
    assert item.base is True and item.news is None
    assert item.missing


def test_partial_first_session_cutoff_cannot_cherry_pick_a_later_roster(engine: Engine) -> None:
    assessment = datetime(2026,11,4,14,45,tzinfo=UTC)
    scan(engine, "assessment", assessment)
    scan(engine, "later", NOW)
    with pytest.raises(ValueError, match="partial first-session"):
        export(engine, NOW, since=NOW)
    preserved = export(engine, NOW, since=assessment)
    assert roster(preserved)[0].key == "assessment:NVDA"


@pytest.mark.parametrize("since", [datetime(2026,11,4,12,tzinfo=UTC), datetime(2026,11,4,14,45,tzinfo=UTC), datetime(2026,11,4,21,tzinfo=UTC)])
def test_boundary_or_nonassessment_since_remains_allowed(engine: Engine, since: datetime) -> None:
    scan(engine)
    assert export(engine, NOW+timedelta(days=1), since=since).as_of == NOW+timedelta(days=1)


def test_legacy_alpha_lab_prices_do_not_supply_strict_marks(engine: Engine) -> None:
    old = snapshot()
    old.pop("news_research_context")
    scan(engine, data=old)
    point(engine, "legacy-price", NOW-timedelta(minutes=1), NOW-timedelta(seconds=10))
    result = audit(engine, NOW)
    assert result.dataset is not None and result.dataset.prices == ()
    assert dict(result.missing)["strict_scan_marks_unavailable"] == 1 and result.price_rows == 0


def test_preassessment_machine_marks_are_known_only_at_scan_completion(engine: Engine) -> None:
    finished = datetime(2026,11,4,14,40,tzinfo=UTC)
    source_at = finished-timedelta(seconds=20)
    mark_scan(engine,"premark",source_at,finished)
    result = export(engine, NOW)
    assert result.opportunities == () and len(result.prices) == 1
    assert result.prices[0].at == source_at and result.prices[0].known_at == finished


def test_machine_marks_have_their_own_explicit_row_bound(engine: Engine) -> None:
    scan(engine)
    with pytest.raises(ValueError, match="mark export truncated"):
        export(engine, NOW, limit=1)


def test_mark_packet_hash_participates_even_when_no_opportunity_is_created(engine: Engine) -> None:
    finished = datetime(2026,11,4,14,40,tzinfo=UTC)
    mark_scan(engine,"before",finished-timedelta(seconds=20),finished)
    first = export(engine,NOW)
    later = finished+timedelta(seconds=10)
    mark_scan(engine,"before2",later-timedelta(seconds=15),later,value=101.)
    second = export(engine,NOW)
    assert first.opportunities == second.opportunities == ()
    assert first.identity != second.identity and len(second.prices) == 2
