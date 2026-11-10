"""A bounded maintenance observer cannot replay billing or invent data availability."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session
from webapp.alpha_lab.db import digest, dumps
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.call_research import coverage, daily_repair, repair_operator
from webapp.call_research.storage import DailyReceipt
from webapp.daily_guardian import service, view

NOW = datetime(2026, 11, 10, 11, tzinfo=UTC)  # 06:00 New York, before the open.
EXPECTED = date(2026, 11, 9)
DAYS = (EXPECTED, date(2026, 11, 6), date(2026, 11, 5), date(2026, 11, 4))
NAMES = ("AAPL", "SPY")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_DATA_GUARDIAN_ENABLED", "true")

    class OperatorClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(repair_operator, "datetime", OperatorClock)
    cfg = coverage.settings().model_copy(update={"persistence_sessions": 3, "query_limit": 1000})
    monkeypatch.setattr(coverage, "settings", lambda: cfg)
    monkeypatch.setattr(coverage, "targets", lambda: NAMES)
    engine = make_engine(f"sqlite:///{tmp_path / 'guardian.db'}")
    for table in (DailyBar, DailyReceipt, repair_operator.RepairRecord):
        table.__table__.create(engine)
    yield engine, tmp_path
    service.STATE.clear()
    engine.dispose()


def seed(engine, *, ready=True, receipts=True):
    days = DAYS if ready else DAYS[1:]
    with Session(engine) as session:
        for ticker in NAMES:
            for day in days:
                payload = dumps(dict(open=100.0, high=102.0, low=98.0, close=101.0, volume=1000.0))
                key = digest((ticker, day))
                session.add(
                    DailyBar(
                        key=key,
                        ticker=ticker,
                        day=day,
                        observed_at=NOW - timedelta(hours=1),
                        payload_json=payload,
                    )
                )
                if receipts:
                    session.add(
                        DailyReceipt(
                            key=key,
                            payload_hash=digest(payload),
                            observed_at=NOW - timedelta(minutes=30),
                        )
                    )
        session.commit()


def invoke(engine, root, *, at=NOW, runner=None):
    return asyncio.run(service.tick(engine, at, clock=lambda: at, runner=runner, root=root))


def no_operator(calls):
    async def forbidden(*args):
        calls.append(args)
        pytest.fail("guarded observation invoked the paid operator")

    return forbidden


def fake_provider(monkeypatch, calls, *, outcome="partial", error=None):
    async def run(engine, output, *, expected_day, clock):
        # A different Session must see durable ownership before provider work.
        with Session(engine) as session:
            claims = session.scalars(
                select(repair_operator.RepairRecord).where(
                    repair_operator.RepairRecord.kind == "claimed"
                )
            ).all()
            assert any(row.request_id == output.name for row in claims)
        calls.append(output.name)
        if error:
            raise error
        output.mkdir(parents=True, exist_ok=False)
        return dict(
            status=outcome,
            expected_day=str(EXPECTED),
            before_ready=0,
            after_ready=0,
            expected=2,
            http_calls=1,
            remaining=29999,
            rows=[],
        )

    monkeypatch.setattr(service, "guarded_run", run)


def test_disabled_loop_never_opens_a_database(monkeypatch):
    monkeypatch.setenv("DAILY_DATA_GUARDIAN_ENABLED", "false")
    monkeypatch.setattr(service, "configured_engine", lambda *_: pytest.fail("disabled DB access"))
    asyncio.run(service.loop("unused", max_cycles=1))


def test_default_is_disabled(monkeypatch):
    monkeypatch.delenv("DAILY_DATA_GUARDIAN_ENABLED", raising=False)
    assert service.enabled() is False


def test_verified_full_coverage_costs_no_provider_call_or_write(env):
    engine, root = env
    seed(engine)
    writes = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE"))
            else None
        ),
    )
    calls = []
    result = invoke(engine, root, runner=no_operator(calls))
    assert result["status"] == "ready"
    assert not calls and not writes


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 11, 10, 9, 59, 59, tzinfo=UTC),
        datetime(2026, 11, 10, 14, 15, tzinfo=UTC),
        datetime(2026, 11, 10, 20, tzinfo=UTC),
    ],
)
def test_outside_fixed_window_never_calls_operator(env, at):
    engine, root = env
    seed(engine, ready=False)
    calls = []
    assert invoke(engine, root, at=at, runner=no_operator(calls))["status"] == "outside_window"
    assert not calls


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 11, 7, 11, tzinfo=UTC),  # Saturday.
        datetime(2026, 11, 26, 11, tzinfo=UTC),  # Thanksgiving.
        datetime(2040, 11, 10, 11, tzinfo=UTC),  # Beyond the verified calendar.
    ],
)
def test_unknown_calendar_or_non_session_is_not_permission_to_fetch(env, at):
    engine, root = env
    seed(engine, ready=False)
    calls = []
    result = invoke(engine, root, at=at, runner=no_operator(calls))
    assert result["status"] in {"calendar_unknown", "closed_day"}
    assert not calls


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_requests", 83),
        ("quota_reserve", 4999),
        ("spacing_seconds", 0.5),
        ("timeout_seconds", 31),
        ("history_sessions", 61),
    ],
)
def test_existing_operator_budget_drift_blocks_before_provider(env, monkeypatch, name, value):
    engine, root = env
    seed(engine, ready=False)
    cfg = daily_repair.settings().model_copy(update={name: value})
    monkeypatch.setattr(daily_repair, "settings", lambda: cfg)
    calls = []
    assert invoke(engine, root, runner=no_operator(calls))["status"] == "config_drift"
    assert not calls


def test_raw_complete_bars_without_physical_receipts_are_not_verified_coverage(env):
    engine, root = env
    seed(engine, receipts=False)
    calls = []
    result = invoke(engine, root, runner=no_operator(calls))
    assert result["status"] == "unverified"
    assert not calls


def test_tampered_required_receipt_cannot_authorize_a_repair(env):
    engine, root = env
    seed(engine)
    with engine.begin() as connection:
        connection.execute(
            update(DailyReceipt)
            .where(DailyReceipt.key == digest(("AAPL", EXPECTED)))
            .values(payload_hash="0" * 64)
        )
    calls = []
    assert invoke(engine, root, runner=no_operator(calls))["status"] == "unverified"
    assert not calls


def test_missing_storage_never_creates_tables_or_invokes_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_DATA_GUARDIAN_ENABLED", "true")
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    calls = []
    result = invoke(engine, tmp_path, runner=no_operator(calls))
    assert result["status"] == "storage_unavailable"
    assert not calls and inspect(engine).get_table_names() == []
    engine.dispose()


def test_one_durable_operator_claim_before_provider_and_no_retry_after_partial(env, monkeypatch):
    engine, root = env
    seed(engine, ready=False)
    calls = []
    fake_provider(monkeypatch, calls)
    first = invoke(engine, root)
    second = invoke(engine, root)
    assert len(calls) == 1
    assert calls[0] == "daily_guardian_v1_20261109"
    assert first["status"] == "operator_finished"
    assert second["status"] == "already_claimed"


def test_operator_exception_is_recorded_and_never_automatically_replayed(env, monkeypatch):
    engine, root = env
    seed(engine, ready=False)
    calls = []
    fake_provider(monkeypatch, calls, error=RuntimeError("sensitive native source detail"))
    invoke(engine, root)
    result = invoke(engine, root)
    assert len(calls) == 1 and result["status"] == "already_claimed"
    assert "sensitive native source detail" not in json.dumps(service.safe_status())
    with Session(engine) as session:
        body = session.scalar(
            select(repair_operator.RepairRecord.payload_json).where(
                repair_operator.RepairRecord.kind == "result"
            )
        )
    assert body and "sensitive native source detail" not in body


def test_restart_reuses_durable_ownership_not_process_memory(env, monkeypatch):
    engine, root = env
    seed(engine, ready=False)
    calls = []
    fake_provider(monkeypatch, calls)
    invoke(engine, root)
    url = str(engine.url)
    engine.dispose()
    restarted = make_engine(url)
    try:
        invoke(restarted, root)
        assert len(calls) == 1
    finally:
        restarted.dispose()


def test_orphaned_own_claim_is_never_replayed(env):
    engine, root = env
    seed(engine, ready=False)
    repair_operator.record(
        engine,
        "daily_guardian_v1_20261109",
        "claimed",
        {"status": "claimed"},
        NOW - timedelta(minutes=5),
    )
    calls = []
    result = invoke(engine, root, runner=no_operator(calls))
    assert result["status"] == "already_claimed"
    assert not calls


def test_pending_manual_run_holds_guardian_even_with_missing_prices(env):
    engine, root = env
    seed(engine, ready=False)
    repair_operator.record(
        engine,
        "manual_current_capture",
        "claimed",
        {"status": "claimed"},
        NOW - timedelta(minutes=1),
    )
    calls = []
    result = invoke(engine, root, runner=no_operator(calls))
    assert result["status"] == "manual_pending"
    assert not calls


def test_status_build_is_select_only_and_contains_no_raw_packets(env, monkeypatch):
    engine, _root = env
    seed(engine)
    writes = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
            else None
        ),
    )
    monkeypatch.setattr(repair_operator, "once", no_operator([]))
    report = view.build(engine, NOW)
    assert not writes
    assert "compressed_payload" not in str(report) and "Bearer " not in str(report)


def test_limited_coverage_stops_without_operator(env, monkeypatch):
    engine, root = env
    seed(engine)
    cfg = coverage.settings().model_copy(update={"query_limit": 2})
    monkeypatch.setattr(coverage, "settings", lambda: cfg)
    calls = []
    assert invoke(engine, root, runner=no_operator(calls))["status"] == "limited"
    assert not calls


def test_invalid_required_price_is_not_permission_to_replace_it(env):
    engine, root = env
    seed(engine)
    key = digest(("AAPL", EXPECTED))
    payload = dumps(dict(open=100.0, high=90.0, low=98.0, close=101.0, volume=1000.0))
    with engine.begin() as connection:
        connection.execute(update(DailyBar).where(DailyBar.key == key).values(payload_json=payload))
        connection.execute(
            update(DailyReceipt).where(DailyReceipt.key == key).values(payload_hash=digest(payload))
        )
    calls = []
    assert invoke(engine, root, runner=no_operator(calls))["status"] == "invalid_data"
    assert not calls


def test_future_physical_receipt_cannot_authorize_a_request(env):
    engine, root = env
    seed(engine)
    with engine.begin() as connection:
        connection.execute(
            update(DailyReceipt)
            .where(DailyReceipt.key == digest(("AAPL", EXPECTED)))
            .values(observed_at=NOW + timedelta(hours=1))
        )
    calls = []
    assert invoke(engine, root, runner=no_operator(calls))["status"] == "unverified"
    assert not calls


@pytest.mark.parametrize("outcome", ["partial", "stopped", "failed"])
def test_prior_manual_attempt_for_same_settled_day_is_not_automatically_retried(env, outcome):
    engine, root = env
    seed(engine, ready=False)
    repair_operator.record(
        engine,
        "manual_same_settled_day",
        "claimed",
        {"status": "claimed"},
        NOW - timedelta(minutes=5),
    )
    repair_operator.record(
        engine,
        "manual_same_settled_day",
        "result",
        {"status": outcome, "expected_day": str(EXPECTED)},
        NOW - timedelta(minutes=4),
    )
    calls = []
    expected_status = "ownership_unverified" if outcome == "failed" else "manual_completed"
    assert invoke(engine, root, runner=no_operator(calls))["status"] == expected_status
    assert not calls


def test_completed_manual_attempt_for_older_day_does_not_hide_new_gap(env, monkeypatch):
    engine, root = env
    seed(engine, ready=False)
    repair_operator.record(
        engine,
        "manual_prior_settled_day",
        "claimed",
        {"status": "claimed"},
        NOW - timedelta(minutes=5),
    )
    repair_operator.record(
        engine,
        "manual_prior_settled_day",
        "result",
        {"status": "partial", "expected_day": "2026-11-06"},
        NOW - timedelta(minutes=4),
    )
    calls = []
    fake_provider(monkeypatch, calls)
    invoke(engine, root)
    assert calls == ["daily_guardian_v1_20261109"]


def test_simultaneous_guardian_observers_share_one_paid_owner(env):
    engine, root = env
    seed(engine, ready=False)
    calls = []

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(bound, request_id, _output):
            won = repair_operator.record(bound, request_id, "claimed", {"status": "claimed"}, NOW)
            if not won:
                return False
            calls.append(request_id)
            started.set()
            await release.wait()
            repair_operator.record(
                bound,
                request_id,
                "result",
                {
                    "status": "partial",
                    "expected_day": str(EXPECTED),
                    "http_calls": 1,
                },
                NOW,
            )
            return True

        first = asyncio.create_task(
            service.tick(engine, NOW, clock=lambda: NOW, runner=runner, root=root)
        )
        await asyncio.wait_for(started.wait(), 2)
        try:
            other = await asyncio.wait_for(
                service.tick(engine, NOW, clock=lambda: NOW, runner=runner, root=root), 2
            )
            assert other["status"] in {"already_claimed", "maintenance_busy", "manual_pending"}
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.wait_for(first, 2)

    asyncio.run(scenario())
    assert len(calls) == 1


def test_manual_entry_cannot_overlap_a_guardian_operation(env, monkeypatch):
    engine, root = env
    seed(engine, ready=False)
    monkeypatch.setenv("DAILY_REPAIR_REQUEST", "manual_contending_with_guardian")
    calls = []

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(bound, request_id, _output):
            won = repair_operator.record(bound, request_id, "claimed", {"status": "claimed"}, NOW)
            if not won:
                return False
            calls.append(request_id)
            started.set()
            await release.wait()
            repair_operator.record(
                bound,
                request_id,
                "result",
                {
                    "status": "partial",
                    "expected_day": str(EXPECTED),
                    "http_calls": 1,
                },
                NOW,
            )
            return True

        async def pause(_seconds):
            await asyncio.sleep(0)

        first = asyncio.create_task(
            service.tick(engine, NOW, clock=lambda: NOW, runner=runner, root=root)
        )
        await asyncio.wait_for(started.wait(), 2)
        try:
            await asyncio.wait_for(
                service.manual_requested(
                    str(engine.url),
                    clock=lambda: NOW,
                    root=root,
                    runner=runner,
                    sleep=pause,
                    max_wait_cycles=1,
                ),
                2,
            )
            assert calls == ["daily_guardian_v1_20261109"]
        finally:
            release.set()
            await asyncio.wait_for(first, 2)

    asyncio.run(scenario())


def test_actual_request_hook_stops_after_spacing_crosses_window(env, monkeypatch):
    from email.utils import format_datetime
    from types import SimpleNamespace

    import httpx
    from webapp.history_capture import capture

    engine, root = env
    seed(engine, ready=False)
    state = SimpleNamespace(at=NOW.replace(hour=14, minute=14, second=59, microsecond=400000))
    wires = []

    class DynamicClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.at.astimezone(tz) if tz is not None else state.at.replace(tzinfo=None)

    monkeypatch.setattr(repair_operator, "datetime", DynamicClock)
    monkeypatch.setattr(capture, "datetime", DynamicClock)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-provider-key")

    async def spacing(seconds):
        state.at += timedelta(seconds=seconds)

    monkeypatch.setattr(capture.asyncio, "sleep", spacing)

    def response(request):
        wires.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    dict(
                        date=str(day),
                        market_time="r",
                        open="100",
                        high="102",
                        low="98",
                        close="101",
                        volume="1000",
                    )
                    for day in DAYS
                ]
            },
            headers={
                "date": format_datetime(state.at, usegmt=True),
                "x-uw-daily-req-count": str(len(wires)),
                "x-uw-token-req-limit": "30000",
            },
        )

    actual_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        return actual_client(*args, **kwargs)

    monkeypatch.setattr(service.httpx, "AsyncClient", mock_client)
    result = asyncio.run(service.tick(engine, state.at, clock=lambda: state.at, root=root))
    assert len(wires) == 1
    assert state.at.time() >= datetime(2026, 11, 10, 14, 15).time()
    assert result["status"] == "operator_finished"
    with Session(engine) as session:
        records = session.scalars(
            select(repair_operator.RepairRecord).where(
                repair_operator.RepairRecord.kind == "result"
            )
        ).all()
    assert len(records) == 1
    body = json.loads(records[0].payload_json)
    assert body["status"] == "stopped" and body["reason"] == "guardian_window_or_session_changed"
    asyncio.run(service.tick(engine, state.at, clock=lambda: state.at, root=root))
    assert len(wires) == 1


def test_actual_request_hook_blocks_clock_rollback_before_first_wire(env, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from webapp.history_capture import capture

    engine, root = env
    seed(engine, ready=False)
    state = SimpleNamespace(at=NOW)
    wires = []

    class DynamicClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.at.astimezone(tz) if tz is not None else state.at.replace(tzinfo=None)

    class ReversingClient(capture.Client):
        async def get(self, path, params):
            state.at -= timedelta(seconds=1)
            return await super().get(path, params)

    monkeypatch.setattr(repair_operator, "datetime", DynamicClock)
    monkeypatch.setattr(capture, "datetime", DynamicClock)
    monkeypatch.setattr(service, "Client", ReversingClient)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-provider-key")

    def response(request):
        wires.append(request.url.path)
        return httpx.Response(500)

    actual_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        return actual_client(*args, **kwargs)

    monkeypatch.setattr(service.httpx, "AsyncClient", mock_client)
    asyncio.run(service.tick(engine, state.at, clock=lambda: state.at, root=root))
    assert wires == []
    with Session(engine) as session:
        bodies = session.scalars(
            select(repair_operator.RepairRecord.payload_json).where(
                repair_operator.RepairRecord.kind == "result"
            )
        ).all()
    assert len(bodies) == 1
    body = json.loads(bodies[0])
    assert body["status"] == "stopped" and body["reason"] == "physical_clock_reversed"
    asyncio.run(service.tick(engine, NOW, clock=lambda: NOW, root=root))
    assert wires == []
