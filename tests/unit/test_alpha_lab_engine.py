"""Source observations → frozen prospective cohorts → matched future outcomes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp.alpha_lab import archive, db, engine, job, readers
from webapp.alpha_lab.settings import load_settings
from webapp.board.atm import AlfaAtm, ensure_atm_tables
from webapp.board.daily_close import (
    AlfaDailyBar,
    AlfaDailyClose,
    ensure_daily_bar_tables,
    ensure_daily_close_tables,
)
from webapp.board.db import make_engine
from webapp.board.lag_record import AlfaLagRecord, ensure_lag_record_tables
from webapp.live_alpha import job as legacy_job
from webapp.live_alpha.store import ScanRow, ensure_live_tables

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_alpha_lab_flow import payload
from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)
CAL = load_live_alpha_settings().calendar
CFG = load_settings()
SCAN = ScanRow("scan", NOW, NOW, "LIVE", "ok", "legacy", {"universe": ["X", "Y"]})


@pytest.fixture(autouse=True)
def reset_globals() -> Iterator[None]:
    states = (archive.STATE, engine.STATE, legacy_job.LAST_CYCLE)
    old = [dict(s) for s in states]
    yield
    for s, value in zip(states, old, strict=True):
        s.clear()
        s.update(value)


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[tuple[Engine, archive.FlowArchive]]:
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    connection = make_engine(url)
    db.ensure(connection)
    ensure_atm_tables(connection)
    ensure_daily_bar_tables(connection)
    ensure_daily_close_tables(connection)
    ensure_lag_record_tables(connection)
    ensure_live_tables(connection)
    symbols = ("X", "Y", "P1", "P2", "P3", "SPY")
    days = []
    day = NOW.date()
    while len(days) < 32:
        day -= timedelta(days=1)
        if is_session_day(day, CAL):
            days.append(day)
    seen = NOW - timedelta(days=1)
    with Session(connection) as s:
        for ticker in symbols:
            for day in sorted(days):
                price = (
                    500.0
                    if ticker == "SPY"
                    else 105.0
                    if ticker.startswith("P") and day == days[0]
                    else 100.0
                )
                s.add(
                    AlfaDailyBar(
                        ticker=ticker,
                        day=day,
                        open=price,
                        high=price + 1,
                        low=price - 1,
                        close=price,
                        fetched_at=seen,
                    )
                )
                s.add(AlfaDailyClose(ticker=ticker, day=day, close=price, fetched_at=seen))
        for ticker in ("X", "Y"):
            s.add(
                AlfaAtm(
                    ticker=ticker,
                    expiry=date(2026, 11, 20),
                    strike=100,
                    stock_price=101,
                    fetched_at=NOW,
                    trade_date=NOW.date(),
                )
            )
            s.add(
                AlfaLagRecord(
                    day=NOW.date(),
                    ticker=ticker,
                    source="hand",
                    basket_id="test",
                    peers="P1,P2,P3",
                    start=date(2026, 10, 19),
                    end=date(2026, 10, 26),
                    window_sessions=5,
                    own_pct=0,
                    peers_pct=5,
                    residual_pct=-5,
                    lagging=True,
                    leading=False,
                    version="baskets_v1+peers_v1",
                    recorded_at=NOW - timedelta(minutes=1),
                )
            )
        s.commit()
    capture = archive.FlowArchive(url)
    yield connection, capture
    capture.close()
    connection.dispose()


def capture_window(capture: archive.FlowArchive, ticker: str, flow: bool) -> None:
    for i in range(16):
        records = (
            [
                payload(
                    id=f"{ticker}{n}",
                    option_chain=f"{ticker}261120C0010{n}000",
                    total_premium="200000",
                    total_ask_side_prem="180000",
                    total_bid_side_prem="20000",
                )
                for n in (0, 5)
            ]
            if flow and i == 15
            else []
        )
        capture.write(ticker, records, NOW - timedelta(seconds=900 - i * 60))


def test_common_eligibility_locks_first_arm_and_membership_across_restart(
    seeded: tuple[Engine, archive.FlowArchive],
) -> None:
    e, capture = seeded
    capture_window(capture, "X", True)
    capture_window(capture, "Y", False)
    result = engine.observe(e, SCAN, NOW)
    assert result["eligible_this_cycle"] == 2 and result["flow_this_cycle"] == 1
    with Session(e) as s:
        cohorts = {r.ticker: r for r in s.scalars(select(db.Cohort))}
        assert cohorts["X"].arm == "flow" and cohorts["Y"].arm == "control"
        frozen = cohorts["Y"].payload_json
        assert json.loads(frozen)["peers"] == ["P1", "P2", "P3"]
    capture.write(
        "Y",
        [
            payload(
                id=f"new{i}",
                option_chain=f"Y261120C0010{i}000",
                total_premium="500000",
                total_ask_side_prem="500000",
                total_bid_side_prem="0",
            )
            for i in (0, 5)
        ],
        NOW + timedelta(seconds=20),
    )
    later = ScanRow("later", NOW, NOW, "LIVE", "ok", "legacy", SCAN.snapshot)
    engine.observe(e, later, NOW + timedelta(seconds=20))
    with Session(e) as s:
        assert s.scalar(select(func.count()).select_from(db.Cohort)) == 2
        assert s.scalar(select(db.Cohort.payload_json).where(db.Cohort.ticker == "Y")) == frozen
        assert s.scalar(select(func.count()).select_from(db.Audit)) == 4
    engine.observe(e, later, NOW + timedelta(seconds=20))
    with Session(e) as s:
        assert s.scalar(select(func.count()).select_from(db.Audit)) == 4


def test_missing_capture_and_future_inputs_never_form_control(
    seeded: tuple[Engine, archive.FlowArchive],
) -> None:
    e, capture = seeded
    engine.observe(e, SCAN, NOW)
    with Session(e) as s:
        assert s.scalar(select(func.count()).select_from(db.Cohort)) == 0
    capture_window(capture, "X", True)
    capture_window(capture, "Y", False)
    with e.begin() as c:
        c.execute(update(AlfaAtm).values(fetched_at=NOW + timedelta(seconds=1)))
    result = engine.observe(
        e, ScanRow("future", NOW, NOW, "LIVE", "ok", "legacy", SCAN.snapshot), NOW
    )
    assert result["eligible_this_cycle"] == 0
    with Session(e) as s:
        reasons = [json.loads(x)["reasons"] for x in s.scalars(select(db.Audit.payload_json))]
        assert any("capture_warmup" in r for r in reasons)
        assert any("spot_unavailable_or_stale" in r for r in reasons)


def test_bars_are_availability_bounded_and_missing_peer_stays_unknown(
    seeded: tuple[Engine, archive.FlowArchive],
) -> None:
    e, capture = seeded
    capture_window(capture, "X", True)
    capture_window(capture, "Y", False)
    engine.observe(e, SCAN, NOW)
    target = datetime(2026, 11, 4, 22, tzinfo=UTC)
    dates = [
        date(2026, 10, 28),
        date(2026, 10, 29),
        date(2026, 10, 30),
        date(2026, 11, 2),
        date(2026, 11, 3),
    ]
    with Session(e) as s:
        for ticker in ("X", "Y", "P1", "P2", "SPY"):  # P3 deliberately missing
            for day in dates:
                p = 100.0 if ticker != "SPY" else 500.0
                s.add(
                    AlfaDailyBar(
                        ticker=ticker,
                        day=day,
                        open=p,
                        high=p + 2,
                        low=p - 1,
                        close=p + 1,
                        fetched_at=target,
                    )
                )
        s.commit()
    assert readers.bars_at(e, ["X"], NOW)["X"][-1].day == date(2026, 10, 26)
    progress = engine.update_outcomes(e, target)
    assert progress["unknown_prices"] == 6
    with Session(e) as s:
        assert s.scalar(select(func.count()).select_from(db.Outcome)) == 0
    with Session(e) as s:
        for day in dates:
            s.add(
                AlfaDailyBar(
                    ticker="P3", day=day, open=100, high=102, low=99, close=101, fetched_at=target
                )
            )
        s.commit()
    assert engine.update_outcomes(e, target)["priced_new"] == 6
    assert engine.update_outcomes(e, target)["priced_new"] == 0
    with Session(e) as s:
        assert s.scalar(select(func.count()).select_from(db.Outcome)) == 6


@pytest.mark.asyncio
async def test_shadow_failure_is_visible_and_does_not_raise_to_legacy(
    seeded: tuple[Engine, archive.FlowArchive], monkeypatch: pytest.MonkeyPatch
) -> None:
    e, _ = seeded

    def fail(*_: Any) -> Any:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(job, "observe", fail)
    await job.run_shadow(e, SCAN, NOW)
    assert engine.STATE["status"] == "failed" and engine.STATE["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_timed_out_database_thread_cannot_overlap_next_research_cycle(
    seeded: tuple[Engine, archive.FlowArchive], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    e, _ = seeded
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def slow(*_: Any) -> None:
        calls.append(1)
        try:
            assert release.wait(timeout=2)
        finally:
            finished.set()

    monkeypatch.setattr(job, "observe", slow)
    monkeypatch.setattr(job, "load_settings", lambda: SimpleNamespace(cycle_timeout_seconds=0.05))
    try:
        await job.run_shadow(e, SCAN, NOW)
        assert engine.STATE["error"] == "TimeoutError"
        await job.run_shadow(e, SCAN, NOW)
        assert engine.STATE["error"] == "ObservationBusyError" and len(calls) == 1
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
    # Acquiring the lock waits for the worker's finally block, not a guessed sleep.
    assert await asyncio.to_thread(job._RUNNING.acquire, True, 1)
    job._RUNNING.release()
    await job.run_shadow(e, SCAN, NOW)
    assert len(calls) == 2


def test_private_lab_route_renders_cold_start_and_observed_flow(
    seeded: tuple[Engine, archive.FlowArchive], monkeypatch: pytest.MonkeyPatch
) -> None:
    from webapp import main

    e, capture = seeded
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/laboratuvar").status_code == 401
    empty = client.get("/laboratuvar", headers=headers)
    assert empty.status_code == 200 and "İlk araştırma döngüsü bekleniyor" in empty.text
    capture_window(capture, "X", True)
    capture_window(capture, "Y", False)
    engine.observe(e, SCAN, NOW)
    response = client.get("/laboratuvar", headers=headers)
    assert (
        response.status_code == 200
        and "data-lab-flow" in response.text
        and "Akış destekli" in response.text
    )
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_supervisor_calls_shadow_after_legacy_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webapp import gamma

    seen = []

    async def cycle(*_: Any, **__: Any) -> ScanRow:
        seen.append("legacy")
        return SCAN

    async def shadow(*_: Any) -> None:
        seen.append("shadow")

    monkeypatch.setattr(legacy_job, "run_cycle", cycle)
    monkeypatch.setattr(legacy_job, "run_shadow", shadow)
    monkeypatch.setattr(gamma, "GammaRepo", lambda *_: SimpleNamespace(latest=lambda: {}))
    await legacy_job.live_alpha_loop(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        client_factory=lambda: SimpleNamespace(),
        clock=lambda: NOW,
        max_cycles=1,
    )
    assert seen == ["legacy", "shadow"]
