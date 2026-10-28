"""Causal opening advice, atomic journal and honest first-entry reference marks."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp import main
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.desk.positions import Position
from webapp.live_alpha import alerts as legacy
from webapp.live_alpha import store
from webapp.opening import alerts, observer, outcomes, policy, storage, view
from webapp.opening.model import Advice, clock, next_wait, settings, utc

from tests.unit._webapp_auth import set_web_auth
from uoa_detector.live_alpha.settings import load_live_alpha_settings

T0 = datetime(2026, 10, 28, 13, 45, tzinfo=UTC)  # 09:45 ET / 16:45 TR
BASE = load_live_alpha_settings()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    e = make_engine(f"sqlite:///{tmp_path / 'opening.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    return e


def source(at: datetime = T0, identity: str = "scan", price: float = 100) -> store.ScanRow:
    card = dict(
        ticker="NVDA",
        direction="up",
        recommendation="BUY",
        readiness="READY",
        market_mode="LIVE",
        opportunity_id=f"{at.date()}:NVDA:up",
        decision_as_of=at.isoformat(),
        headline="NVDA source BUY",
        plain_reason="Akış ve fiyat teyidi.",
        policy_version=BASE.policy_version,
        profile_sha256=BASE.profile_sha256,
        instrument={"preferred": "stock"},
        stock_plan=dict(
            entry_ref=round(price, 2),
            entry_ref_at=at.isoformat(),
            stop=price - 3,
            target=price + 6,
            shares=20,
            planned_risk_usd=60,
        ),
        options=[],
        option_evidence="$600k bullish flow",
        price_evidence="SPY-relative +0.8pp",
        news_evidence="Başarılı kontrol.",
        counter_argument="Akış hedge olabilir.",
        invalidation="Stop altı.",
    )
    snapshot = dict(
        policy_version=BASE.policy_version,
        profile_sha256=BASE.profile_sha256,
        cards=[card],
        flow_current=True,
        price_observations={
            "NVDA": {"spot": price, "source_at": at.isoformat(), "session": at.date().isoformat()},
            "SPY": {"spot": 500, "source_at": at.isoformat(), "session": at.date().isoformat()},
        },
        flow_observations={"NVDA": {"last_ts": at.isoformat()}},
        news_observations={"NVDA": {"state": "checked_found", "checked_at": at.isoformat()}},
    )
    return store.ScanRow(identity, at, at, "LIVE", "ok", "run", snapshot)


def project(
    scan: store.ScanRow | None = None, at: datetime = T0, conflicts: set[str] | None = None
) -> Advice:
    row = scan or source(at)
    return policy.project(row, row.snapshot["cards"][0], at, clock(at), conflicts or set())


def persist(e: Engine, row: store.ScanRow, at: datetime | None = None) -> None:
    store.write_scan(e, row, BASE.policy_version)
    observer.observe(e, at or row.finished_at, wall_clock=lambda: at or row.finished_at)


def rows(e: Engine, model: Any) -> list[Any]:
    with Session(e) as s:
        return list(s.scalars(select(model)))


@pytest.mark.parametrize(
    ("at", "phase", "checkpoint"),
    [
        (T0 - timedelta(minutes=36), "before", None),
        (T0 - timedelta(minutes=35), "preparation", "preparation"),
        (T0 - timedelta(minutes=16), "preparation", "preparation"),
        (T0 - timedelta(minutes=15), "settling", None),
        (T0 - timedelta(seconds=1), "settling", None),
        (T0, "assessment", "post_open"),
        (T0 + timedelta(minutes=10), "assessment", None),
        (T0.replace(hour=20, minute=0), "closed", None),
    ],
)
def test_clock_boundaries(at: datetime, phase: str, checkpoint: str | None) -> None:
    c = clock(at)
    assert (c.phase, c.checkpoint) == (phase, checkpoint)
    assert view.tr_time(c.opening) == "16:30"
    assert view.tr_time(c.assessment) == "16:45"


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 10, 31), date(2028, 10, 28)])
def test_holidays_weekend_unknown_year(day: date) -> None:
    c = clock(datetime.combine(day, T0.time(), UTC))
    assert c.phase == "closed" and c.opening is None


def test_dst_early_close_and_anchor_sleep() -> None:
    winter = clock(datetime(2026, 11, 2, 14, 45, tzinfo=UTC))
    assert view.tr_time(winter.opening) == "17:30"
    early = clock(datetime(2026, 11, 27, 18, 0, tzinfo=UTC))
    assert early.phase == "closed" and view.tr_time(early.closing) == "21:00"
    assert next_wait(T0 - timedelta(seconds=2), 300) == 2
    assert next_wait(T0, 300) == 300


def test_ready_and_bounded_expiry() -> None:
    a = project()
    assert a.action == "BUY" and a.plan and a.plan.quantity == 20
    assert a.expires_at == T0 + timedelta(seconds=300)
    assert len(a.evidence) == 3 and a.source_card_hash
    assert project(source(price=100.123)).action == "BUY"  # source policy rounds cents


@pytest.mark.parametrize(
    "mutation",
    [
        "scan_old",
        "scan_future",
        "finished_future",
        "pre15_scan",
        "old_day",
        "not_ready",
        "wrong_policy",
        "missing_profile",
        "decision_future",
        "spot_old",
        "spot_future",
        "spot_premarket",
        "missing_spot",
        "flow_old",
        "flow_future",
        "flow_premarket",
        "flow_not_current",
        "missing_flow",
        "news_failed",
        "news_missing",
        "news_future",
        "news_old",
        "quantity_zero",
        "quantity_fraction",
        "risk_zero",
        "risk_missing",
        "stop_wrong",
        "missing_plan",
        "no_instrument",
        "missing_instrument",
        "price_changed",
        "price_time_changed",
        "unowned_stock_short",
    ],
)
def test_critical_gates_wait(mutation: str) -> None:
    row = source()
    card = row.snapshot["cards"][0]
    price = row.snapshot["price_observations"]["NVDA"]
    flow = row.snapshot["flow_observations"]["NVDA"]
    news = row.snapshot["news_observations"]["NVDA"]
    if mutation == "scan_old":
        row = replace(row, started_at=T0 - timedelta(seconds=301))
    elif mutation == "scan_future":
        row = replace(row, started_at=T0 + timedelta(seconds=1))
    elif mutation == "finished_future":
        row = replace(row, finished_at=T0 + timedelta(seconds=1))
    elif mutation == "pre15_scan":
        row = replace(row, started_at=T0 - timedelta(seconds=1))
    elif mutation == "old_day":
        card["opportunity_id"] = "2026-10-27:NVDA:up"
    elif mutation == "not_ready":
        card["readiness"] = "TRIGGER_PENDING"
    elif mutation == "wrong_policy":
        card["policy_version"] = "different"
    elif mutation == "missing_profile":
        card.pop("profile_sha256")
    elif mutation == "decision_future":
        card["decision_as_of"] = (T0 + timedelta(seconds=1)).isoformat()
    elif mutation in ("spot_old", "spot_future", "spot_premarket"):
        price["source_at"] = (
            T0 - timedelta(seconds=301)
            if mutation == "spot_old"
            else T0 + timedelta(seconds=1)
            if mutation == "spot_future"
            else T0 - timedelta(minutes=16)
        ).isoformat()
    elif mutation == "missing_spot":
        price["spot"] = None
    elif mutation in ("flow_old", "flow_future", "flow_premarket"):
        flow["last_ts"] = (
            T0 - timedelta(seconds=901)
            if mutation == "flow_old"
            else T0 + timedelta(seconds=1)
            if mutation == "flow_future"
            else T0 - timedelta(minutes=16)
        ).isoformat()
    elif mutation == "flow_not_current":
        row.snapshot["flow_current"] = False
    elif mutation == "missing_flow":
        row.snapshot.pop("flow_observations")
    elif mutation == "news_failed":
        news["state"] = "failed"
    elif mutation == "news_missing":
        row.snapshot.pop("news_observations")
    elif mutation in ("news_future", "news_old"):
        news["checked_at"] = (
            T0 + timedelta(seconds=1) if mutation == "news_future" else T0 - timedelta(seconds=901)
        ).isoformat()
    elif mutation == "quantity_zero":
        card["stock_plan"]["shares"] = 0
    elif mutation == "quantity_fraction":
        card["stock_plan"]["shares"] = 0.5
    elif mutation == "risk_zero":
        card["stock_plan"]["planned_risk_usd"] = 0
    elif mutation == "risk_missing":
        card["stock_plan"]["planned_risk_usd"] = None
    elif mutation == "stop_wrong":
        card["stock_plan"]["stop"] = 103
    elif mutation == "missing_plan":
        card["stock_plan"] = None
    elif mutation == "no_instrument":
        card["instrument"] = {"preferred": "none"}
    elif mutation == "missing_instrument":
        card["instrument"] = None
    elif mutation == "price_changed":
        price["spot"] = 101
    elif mutation == "price_time_changed":
        card["stock_plan"]["entry_ref_at"] = (T0 - timedelta(seconds=1)).isoformat()
    elif mutation == "unowned_stock_short":
        card.update(
            direction="down", recommendation="BEARISH_SETUP", opportunity_id="2026-10-28:NVDA:down"
        )
    assert project(row).action == "WAIT"


def option_source() -> store.ScanRow:
    row = source()
    card = row.snapshot["cards"][0]
    card.update(
        direction="down",
        opportunity_id="2026-10-28:NVDA:down",
        recommendation="BEARISH_SETUP",
        instrument={"preferred": "long_put"},
    )
    card["stock_plan"].update(stop=103, target=94)
    card["options"] = [
        dict(
            kind="long_put",
            readiness="READY",
            lots=1,
            max_loss_usd=201.3,
            entry_debit=2,
            roundtrip_cost_pct=10,
            dte=21,
            legs=[
                dict(
                    option_symbol="NVDA261120P00100000", fetched_at=T0.isoformat(), bid=1.9, ask=2.0
                )
            ],
        )
    ]
    return row


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "stale_quote",
        "future_quote",
        "zero_lots",
        "cost",
        "crossed",
        "missing_leg",
        "risk",
        "dte",
        "not_ready",
    ],
)
def test_bearish_is_put_buy_only_and_option_gates(mutation: str) -> None:
    row = option_source()
    option = row.snapshot["cards"][0]["options"][0]
    if mutation == "stale_quote":
        option["legs"][0]["fetched_at"] = (T0 - timedelta(seconds=901)).isoformat()
    elif mutation == "future_quote":
        option["legs"][0]["fetched_at"] = (T0 + timedelta(seconds=1)).isoformat()
    elif mutation == "zero_lots":
        option["lots"] = 0
    elif mutation == "cost":
        option["roundtrip_cost_pct"] = 26
    elif mutation == "crossed":
        option["legs"][0]["bid"] = 3
    elif mutation == "missing_leg":
        option["legs"] = []
    elif mutation == "risk":
        option["max_loss_usd"] = -1
    elif mutation == "dte":
        option["dte"] = 1
    elif mutation == "not_ready":
        option["readiness"] = "QUOTE_PENDING"
    a = project(row)
    assert a.action == ("BUY" if mutation == "valid" else "WAIT")
    assert a.action != "SELL"
    if mutation == "valid":
        assert a.plan and a.plan.instrument == "long_put" and a.plan.quantity == 1


def position(scope: str = "manual", at: datetime = T0) -> Position:
    return Position(
        "p",
        scope,
        "NVDA",
        "stock",
        "up",
        "open",
        10,
        100,
        96,
        at,
        96,
        -40,
        97,
        106,
        -1,
        1000,
        30,
        "tech",
        "EXIT_REVIEW",
        "Stopa ulaşıldı.",
        None,
        "plan",
    )


def test_held_exit_without_first15_delay_and_conflict() -> None:
    at = T0 - timedelta(minutes=10)
    assert policy.exits((position(at=at),), at, clock(at))[0].action == "SELL"
    assert policy.exits((position("paper"),), T0, clock(T0))[0].action == "PAPER_EXIT"
    assert (
        policy.exits((position(at=T0 - timedelta(seconds=301)),), T0, clock(T0))[0].action
        == "REVIEW"
    )
    assert (
        policy.exits((position(),), T0 + timedelta(days=1), clock(T0 + timedelta(days=1)))[0].action
        == "REVIEW"
    )
    assert project(conflicts={"NVDA"}).action == "WAIT"
    assert policy.exits((), T0, clock(T0)) == ()


def test_append_every_scan_changed_plan_one_primary_and_no_page_writes(engine: Engine) -> None:
    persist(engine, source())
    persist(engine, source(T0 + timedelta(seconds=60), "second", 101))
    observer.observe(
        engine, T0 + timedelta(seconds=60), wall_clock=lambda: T0 + timedelta(seconds=60)
    )
    assert len(rows(engine, storage.OpeningCycle)) == 2
    assert len(rows(engine, storage.OpeningDecision)) == 2
    primary = [p for p in rows(engine, storage.OpeningPrimary) if p.kind == "entry"]
    assert len(primary) == 1 and utc(primary[0].published_at) == T0
    before_counts = [len(rows(engine, m)) for m in storage.TABLES]
    desk = view.build(engine, T0 + timedelta(seconds=60))
    assert desk.current and desk.statements[0].advice.plan.entry == 101
    assert len(desk.history) == 2 and desk.count == 2
    assert [len(rows(engine, m)) for m in storage.TABLES] == before_counts
    assert not view.build(engine, T0 + timedelta(seconds=400)).current
    assert not view.build(engine, T0 + timedelta(days=1)).recorded


def test_actual_publication_cannot_be_backdated_and_uncommitted_source_not_used(
    engine: Engine,
) -> None:
    observer.observe(engine, T0, wall_clock=lambda: T0)
    assert not rows(engine, storage.OpeningCycle)
    store.write_scan(engine, source(), BASE.policy_version)
    observer.observe(engine, T0, wall_clock=lambda: T0 + timedelta(seconds=10))
    assert utc(rows(engine, storage.OpeningCycle)[0].published_at) == T0 + timedelta(seconds=10)
    assert view.build(engine, T0).recorded is False
    assert view.build(engine, T0 + timedelta(seconds=10)).recorded


def test_future_scan_not_selected(engine: Engine) -> None:
    store.write_scan(engine, source(T0 + timedelta(seconds=1)), BASE.policy_version)
    assert storage.source(engine, T0) is None


def test_atomic_failure_rolls_back_whole_publication(engine: Engine) -> None:
    def fail(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.startswith("INSERT INTO alfa_opening_decision"):
            raise RuntimeError("injected insert failure")

    event.listen(engine, "before_cursor_execute", fail)
    row = source()
    with pytest.raises(RuntimeError, match="injected"):
        storage.publish(engine, row, T0, clock(T0), (project(row),))
    event.remove(engine, "before_cursor_execute", fail)
    assert all(not rows(engine, m) for m in storage.TABLES)


def test_concurrent_first_writer_wins_complete_cycle(engine: Engine) -> None:
    row = source()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(
            pool.map(
                lambda _: storage.publish(engine, row, T0, clock(T0), (project(row),)), range(4)
            )
        )
    assert sum(result) == 1
    assert len(rows(engine, storage.OpeningDecision)) == 1
    assert len(rows(engine, storage.OpeningObservation)) == 2


def test_checkpoint_membership_first_window_and_no_late_rewrite(engine: Engine) -> None:
    pre = T0 - timedelta(minutes=30)
    persist(engine, source(pre, "pre"))
    later = source(pre + timedelta(minutes=1), "pre2")
    later.snapshot["cards"][0]["ticker"] = "AMD"
    persist(engine, later)
    persist(engine, source())
    late = source(T0 + timedelta(minutes=11), "late")
    late.snapshot["cards"][0]["ticker"] = "MSFT"
    persist(engine, late)
    c = view.build(engine, T0 + timedelta(minutes=11)).checkpoints
    assert c[0].tickers == ("NVDA",) and c[1].tickers == ("NVDA",)
    assert all(p.at for p in c)


def test_missed_checkpoint_never_backfilled(engine: Engine) -> None:
    at = T0 + timedelta(minutes=11)
    persist(engine, source(at))
    assert all("Kaçırıldı" in p.status for p in view.build(engine, at).checkpoints)
    assert not [p for p in rows(engine, storage.OpeningPrimary) if p.kind != "entry"]


def test_no_source_buy_before15_and_expiry_boundary(engine: Engine) -> None:
    for minutes in (-30, -10):
        at = T0 + timedelta(minutes=minutes)
        persist(engine, source(at, str(minutes)))
        assert rows(engine, storage.OpeningDecision)[-1].action == "WAIT"
    assert project(source(), T0 + timedelta(seconds=300)).action == "WAIT"
    assert not [p for p in rows(engine, storage.OpeningPrimary) if p.kind == "entry"]


def add_observation(
    engine: Engine, ticker: str, source_at: datetime, available_at: datetime, value: float
) -> None:
    append(
        engine,
        cast(Table, storage.OpeningObservation.__table__),
        [
            dict(
                key=digest((ticker, source_at)),
                ticker=ticker,
                source_at=source_at,
                available_at=available_at,
                value=value,
                scan_id="future",
            )
        ],
    )


def test_intraday_exact_window_and_spy_pair_not_old_paper_entry(engine: Engine) -> None:
    persist(engine, source())
    at = T0 + timedelta(minutes=15, seconds=10)
    add_observation(engine, "NVDA", at, at, 102)
    add_observation(engine, "SPY", at, at, 505)
    assert outcomes.measure(engine, at) == 1
    result = json.loads(rows(engine, storage.OpeningOutcome)[0].payload_json)
    assert result["entry_reference"] == 100
    assert result["directional_underlying_pct"] == pytest.approx(2)
    assert result["directional_excess_pp"] == pytest.approx(1)
    assert result["status"] == "complete"
    assert outcomes.measure(engine, at) == 0
    assert datetime.fromisoformat(result["entry_published_at"]) == T0


def test_intraday_missing_window_no_late_price_or_next_day_substitute(engine: Engine) -> None:
    persist(engine, source())
    at = T0 + timedelta(minutes=21)
    add_observation(engine, "NVDA", T0 + timedelta(minutes=15), at, 150)  # learned too late
    add_observation(engine, "SPY", at, at, 505)
    outcomes.measure(engine, at)
    mark = json.loads(rows(engine, storage.OpeningOutcome)[0].payload_json)
    assert mark["own_price"] is None and "dayanak" in mark["missing"]
    add_observation(engine, "NVDA", T0 + timedelta(days=1), T0 + timedelta(days=1), 200)
    outcomes.measure(engine, T0 + timedelta(days=1))
    assert json.loads(rows(engine, storage.OpeningOutcome)[0].payload_json) == mark


def daily(engine: Engine, ticker: str, day: date, at: datetime, close: float) -> None:
    cast(Table, DailyBar.__table__).create(engine, checkfirst=True)
    append(
        engine,
        cast(Table, DailyBar.__table__),
        [
            dict(
                key=digest((ticker, day)),
                ticker=ticker,
                day=day,
                observed_at=at,
                payload_json=dumps(
                    dict(open=100, high=max(110, close), low=90, close=close, volume=1000)
                ),
            )
        ],
    )


def test_exact_session_closes_holidays_missing_and_availability(engine: Engine) -> None:
    persist(engine, source())
    close = T0.replace(hour=20, minute=0)
    daily(engine, "NVDA", date(2026, 10, 28), close + timedelta(minutes=1), 103)
    daily(engine, "SPY", date(2026, 10, 28), close + timedelta(minutes=1), 510)
    daily(engine, "NVDA", date(2026, 10, 30), close + timedelta(days=2, minutes=1), 150)
    daily(engine, "SPY", date(2026, 10, 30), close + timedelta(days=2, minutes=1), 550)
    outcomes.measure(engine, close)
    assert "c0" not in [r.horizon for r in rows(engine, storage.OpeningOutcome)]
    outcomes.measure(engine, close + timedelta(days=3))
    marks = {r.horizon: json.loads(r.payload_json) for r in rows(engine, storage.OpeningOutcome)}
    assert marks["c0"]["directional_underlying_pct"] == pytest.approx(3)
    assert "c1" not in marks  # missing Oct29 never replaced with Oct30
    assert "c3" not in marks  # Nov2 not yet observed (weekend excluded)


def test_alert_requires_durable_opening_buy_and_preserves_exit(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_signal = legacy.Signal(
        key="e" * 64,
        ticker="MSFT",
        instrument="stock",
        label="EXIT",
        contracts=(),
        as_of=T0,
        expires_at=T0 + timedelta(minutes=2),
        event_type="exit",
    )
    monkeypatch.setattr(
        legacy,
        "feed",
        lambda e, n: legacy.Feed(
            server_time=n,
            status="current",
            detail="source",
            scan_id="scan",
            signals=(exit_signal,),
            exit_status="ok",
        ),
    )
    assert alerts.feed(engine, T0).signals == (exit_signal,)
    persist(engine, source())
    result = alerts.feed(engine, T0)
    assert [s.event_type for s in result.signals] == ["exit", "entry"]
    assert alerts.feed(engine, T0 + timedelta(seconds=301)).signals == (exit_signal,)
    monkeypatch.setattr(
        legacy,
        "feed",
        lambda e, n: legacy.Feed(
            server_time=n, status="current", detail="other", scan_id="uncommitted"
        ),
    )
    assert not alerts.feed(engine, T0).signals


@pytest.mark.parametrize("path", ["/acilis", "/api/acilis/uyarilar", "/assets/opening-desk.js"])
def test_route_private_and_read_only(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    persist(engine, source())
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: T0)
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get(path).status_code == 401
    initial = [len(rows(engine, m)) for m in storage.TABLES]
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    assert initial == [len(rows(engine, m)) for m in storage.TABLES]
    if path == "/acilis":
        assert (
            "16:45" in response.text and "NVDA" in response.text and "kayma hariç" in response.text
        )


def test_observer_failure_isolated_and_single_flight(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        observer.storage,
        "source",
        lambda e, n, scan_id=None: (_ for _ in ()).throw(RuntimeError("fixture")),
    )
    asyncio.run(observer.run(engine, T0))
    assert observer.STATE["status"] == "failed"
    assert observer._RUNNING.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            observer.observe(engine, T0)
    finally:
        observer._RUNNING.release()


def test_stale_scan_does_not_fake_a_morning_checkpoint(engine: Engine) -> None:
    old = source(T0 - timedelta(days=1))
    store.write_scan(engine, old, BASE.policy_version)
    at = T0 - timedelta(minutes=30)
    observer.observe(engine, at, wall_clock=lambda: at)
    assert not [p for p in rows(engine, storage.OpeningPrimary) if p.kind == "preparation"]
    assert all(r.action == "WAIT" for r in rows(engine, storage.OpeningDecision))


def test_pagination_and_previous_days_remain_accessible(engine: Engine) -> None:
    row = source()
    advice = project(row)
    for index in range(settings().history_limit + 1):
        at = T0 + timedelta(seconds=index)
        storage.publish(engine, replace(row, scan_id=str(index)), at, clock(at), (advice,))
    desk = view.build(engine, T0 + timedelta(seconds=60))
    assert len(desk.history) == settings().history_limit and desk.before
    older = view.build(engine, T0 + timedelta(seconds=60), desk.before)
    assert len(older.history) == 1
    tomorrow = view.build(engine, T0 + timedelta(days=1), journal_day=T0.date())
    assert tomorrow.count == settings().history_limit + 1 and not tomorrow.recorded
    assert len(tomorrow.results) == 1  # five-session marks remain visible later


def test_existing_paper_entry_never_substitutes_for_new_reference(engine: Engine) -> None:
    with Session(engine) as s:
        s.add(
            store.LivePaper(
                paper_id="old",
                opportunity_id="older",
                rec_id="old",
                ticker="NVDA",
                direction="up",
                instrument="stock",
                plan_json="{}",
                state="CLOSED_SIMULATED",
                created_at=T0 - timedelta(days=1),
                entry_at=T0 - timedelta(days=1),
                entry_price=1,
                quantity=10,
                exit_at=T0,
                exit_price=100,
                pnl_usd=990,
            )
        )
        s.commit()
    persist(engine, source())
    at = T0 + timedelta(minutes=15)
    add_observation(engine, "NVDA", at, at, 102)
    add_observation(engine, "SPY", at, at, 505)
    outcomes.measure(engine, at)
    mark = json.loads(rows(engine, storage.OpeningOutcome)[0].payload_json)
    assert mark["entry_reference"] == 100 and mark["directional_underlying_pct"] == pytest.approx(2)


def test_invalid_numbers_and_out_of_order_finish_wait() -> None:
    for value in (True, float("nan"), float("inf"), None, "100"):
        assert policy.number(value) is None
    assert project(replace(source(), finished_at=T0 - timedelta(seconds=1))).action == "WAIT"


def test_first_observation_is_not_overwritten(engine: Engine) -> None:
    row = source()
    storage.publish(engine, row, T0, clock(T0), (project(row),))
    changed = source(identity="other", price=200)
    storage.publish(
        engine,
        changed,
        T0 + timedelta(seconds=1),
        clock(T0 + timedelta(seconds=1)),
        (project(changed),),
    )
    own = [r for r in rows(engine, storage.OpeningObservation) if r.ticker == "NVDA"]
    assert len(own) == 1 and own[0].value == 100


def test_real_source_scan_adds_per_name_inputs_without_new_vendor_paths(tmp_path: Path) -> None:
    from webapp.board.signals import BoardSignalReader
    from webapp.live_alpha.job import run_cycle

    from tests.unit.test_live_alpha_job import BOARD, COSTS, SETTINGS, FakeUW, _seed
    from tests.unit.test_live_alpha_job import T0 as SOURCE_AT

    engine = _seed(f"sqlite:///{tmp_path / 'real-cycle.db'}")
    client = FakeUW()
    row = asyncio.run(
        run_cycle(
            engine, BoardSignalReader(engine=engine), client, SETTINGS, BOARD, COSTS, SOURCE_AT
        )
    )
    assert row.snapshot["flow_observations"]["NVDA"]["last_ts"]
    assert row.snapshot["news_observations"]["NVDA"]["state"] == "checked_found"
    assert set(path for path, _ in client.calls) <= {
        "/api/news/headlines",
        "/api/option-contract/NVDA/option-contracts",
        "/api/stock/NVDA/option-contracts",
    }


def test_newer_cached_price_withdraws_buy_without_rewriting_journal(engine: Engine) -> None:
    from webapp.board.atm import AlfaAtm, ensure_atm_tables

    ensure_atm_tables(engine)
    row = source()
    row.snapshot["cards"][0]["stock_plan"]["chase_limit"] = 101
    persist(engine, row)
    with Session(engine) as s:
        s.add(
            AlfaAtm(
                ticker="NVDA",
                expiry=date(2026, 11, 20),
                strike=100,
                stock_price=102,
                fetched_at=T0,
                trade_date=T0.date(),
            )
        )
        s.commit()
    assert not view.build(engine, T0).statements[0].current
    assert rows(engine, storage.OpeningDecision)[0].action == "BUY"
    assert not [s for s in alerts.feed(engine, T0).signals if s.event_type == "entry"]


def test_concurrent_source_scans_are_journaled_without_stale_entry_primary(engine: Engine) -> None:
    store.write_scan(engine, source(), BASE.policy_version)
    at = T0 + timedelta(seconds=10)
    store.write_scan(engine, source(at, "second", 101), BASE.policy_version)
    observer.observe(engine, at, wall_clock=lambda: at, scan_id="scan")
    observer.observe(engine, at, wall_clock=lambda: at, scan_id="second")
    journal = {
        r.ticker + str(i): r.action for i, r in enumerate(rows(engine, storage.OpeningDecision))
    }
    assert list(journal.values()) == ["WAIT", "BUY"]
    entry = [p for p in rows(engine, storage.OpeningPrimary) if p.kind == "entry"]
    assert len(entry) == 1
    decision = next(
        r for r in rows(engine, storage.OpeningDecision) if r.key == entry[0].decision_key
    )
    assert json.loads(decision.payload_json)["plan"]["entry"] == 101
    assert storage.source(engine, T0, scan_id="second") is None
