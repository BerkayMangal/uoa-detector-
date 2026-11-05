"""Fixed attempt timing and native quote admission cannot invent reference prices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from webapp.timed_measurement.collector import parse_quote, schedule

T = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
DEADLINE = T + timedelta(seconds=300)


def decision(now: datetime, claims: tuple[int, ...] = (), completed: dict[int, datetime] | None = None):
    return schedule(T, DEADLINE, now, claims, completed or {}, offsets=(2, 32, 62), spacing_seconds=30)


def payload(at: datetime = T + timedelta(seconds=2), price: object = "101.25") -> dict[str, Any]:
    return {"data": {"market_time": "regular", "last_trade": {"price": price, "time": int(at.timestamp() * 1000)}}}


def quote(body: object, request: datetime = T + timedelta(seconds=2), receipt: datetime = T + timedelta(seconds=3)):
    return parse_quote("AAPL", body, request, receipt, T, DEADLINE, trade_age_seconds=60)


def test_fixed_attempt_offsets_and_previous_completion_spacing() -> None:
    assert decision(T + timedelta(seconds=1)).state == "pending"
    assert decision(T + timedelta(seconds=2)).ordinal == 1
    assert decision(T + timedelta(seconds=2)).state == "ready"
    first_done = T + timedelta(seconds=10)
    pending = decision(T + timedelta(seconds=39), (1,), {1: first_done})
    assert pending.state == "pending" and pending.due_at == T + timedelta(seconds=40)
    ready = decision(T + timedelta(seconds=40), (1,), {1: first_done})
    assert ready.state == "ready" and ready.ordinal == 2
    assert decision(T + timedelta(seconds=100), (1, 2), {1: first_done, 2: T + timedelta(seconds=70)}).ordinal == 3


def test_claim_without_completion_never_retries_or_skips_ordinal() -> None:
    assert decision(T + timedelta(seconds=100), (1,)).state == "incomplete"
    assert decision(DEADLINE, (1,)).state == "late"
    done = {i: T + timedelta(seconds=i * 35) for i in (1, 2, 3)}
    assert decision(T + timedelta(seconds=120), (1, 2, 3), done).state == "exhausted"


@pytest.mark.parametrize("claims,completed", [((2,), {}), ((1, 1), {}), ((4,), {}), ((), {1: T}), ((1,), {1: DEADLINE})])
def test_corrupt_ownership_or_future_completion_fails_closed(claims, completed) -> None:
    assert decision(T + timedelta(seconds=100), claims, completed).state == "invalid"


def test_quote_is_last_trade_and_accepts_no_echo_when_requested_path_is_bound() -> None:
    body = payload()
    body["data"]["quote"] = {"bid": {"price": "999"}, "ask": {"price": "1000"}}
    got = quote(body)
    assert got.status == "ok" and got.price == 101.25 and got.trade_at == T + timedelta(seconds=2)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", -1, 0, None, "bad"])
def test_nonpositive_nonfinite_or_malformed_quote_price_rejected(value: object) -> None:
    assert quote(payload(price=value)).reason == "last_trade_price"


@pytest.mark.parametrize("damage", ["echo", "phase", "midpoint_only", "seconds", "future", "before_target", "stale", "previous_session", "naive_request", "backward_receipt", "late_receipt"])
def test_source_and_clock_guards_reject_without_price(damage: str) -> None:
    body = payload()
    request, receipt = T + timedelta(seconds=2), T + timedelta(seconds=3)
    if damage == "echo":
        body["data"]["ticker"] = "MSFT"
    elif damage == "phase":
        body["data"]["market_time"] = "premarket"
    elif damage == "midpoint_only":
        del body["data"]["last_trade"]
    elif damage == "seconds":
        body["data"]["last_trade"]["time"] //= 1000
    elif damage == "future":
        body = payload(receipt + timedelta(milliseconds=1))
    elif damage == "before_target":
        body = payload(T - timedelta(milliseconds=1))
    elif damage == "stale":
        receipt = T + timedelta(seconds=63, milliseconds=1)
    elif damage == "previous_session":
        body = payload(T - timedelta(days=1))
    elif damage == "naive_request":
        request = request.replace(tzinfo=None)
    elif damage == "backward_receipt":
        receipt = request - timedelta(milliseconds=1)
    else:
        receipt = DEADLINE + timedelta(milliseconds=1)
    got = quote(body, request, receipt)
    assert got.status == "invalid_source" and got.price is None and got.trade_at is None


def test_halfday_close_and_unknown_calendar_cannot_be_regular_quote() -> None:
    for at in (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), datetime(2028, 11, 5, 15, 0, tzinfo=UTC)):
        got = parse_quote("AAPL", payload(at), at, at, at - timedelta(seconds=1), at + timedelta(seconds=1), trade_age_seconds=60)
        assert got.status == "invalid_source"


@pytest.fixture
def capture(tmp_path, monkeypatch, request):
    from webapp.board.db import make_engine
    from webapp.board.quota_ledger import AlfaUwQuota
    from webapp.timed_measurement import collector, storage
    from webapp.timed_measurement.config import profile_hash
    from webapp.timed_measurement.model import Target, digest

    engine = make_engine(f"sqlite:///{tmp_path / 'capture.db'}")
    storage.ensure(engine)
    with engine.begin() as connection:
        connection.execute(AlfaUwQuota.__table__.insert().values(et_date=T.date(), reserved=0, updated_at=T))
    activation = storage.activate(engine, lambda: T - timedelta(hours=1))
    monkeypatch.setattr(collector, "_discover", lambda *args: None)
    options = getattr(request, "param", 500)
    direction = options.get("direction", "up") if isinstance(options, dict) else "up"
    spy_entry = options.get("spy_entry", 500) if isinstance(options, dict) else options
    target = Target(
        key=digest((activation.key, "first-primary", "m15")),
        activation_key=activation.key, profile_hash=profile_hash(),
        opening_profile_hash=activation.opening_profile_hash,
        primary_key="first-primary", decision_key="first-decision", ticker="AAPL", day=T.date(),
        direction=direction, instrument="stock", published_at=T - timedelta(minutes=15),
        entry_reference=100, entry_source_at=T - timedelta(minutes=15),
        spy_entry=spy_entry, spy_entry_source_at=T - timedelta(minutes=15),
        source_scan_id="original-scan", source_snapshot_hash="snapshot", source_card_hash="card",
        horizon="m15", horizon_minutes=15, target_at=T, deadline=DEADLINE,
        eligible=True, reason="", discovered_at=T,
    )
    storage.save_target(engine, target)
    yield engine, target
    engine.dispose()


class CaptureClock:
    def __init__(self, at=T + timedelta(seconds=2)):
        self.at = at

    def __call__(self):
        return self.at


def transport(clock, handler):
    import httpx
    from pydantic import SecretStr
    from webapp.timed_measurement.transport import QuoteTransport

    from uoa_detector.calibration import load_default_profile

    return QuoteTransport(api_key=SecretStr("hermetic-fixture"),
                          settings=load_default_profile().data_sources.unusual_whales,
                          timeout_seconds=8, max_response_bytes=65536, clock=clock,
                          transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_actual_transport_and_storage_pair_is_durable_and_never_replayed(capture) -> None:
    import httpx
    from sqlalchemy import select
    from webapp.board.db import session_factory
    from webapp.board.quota_ledger import AlfaUwQuota
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        result = await tick(engine, client, clock(), clock=clock)
        assert result.attempts == result.assessed == 1
        original = storage.assessment(engine, target.key, clock())
        assert original and original.status == "observed"
        assert original.return_pct == pytest.approx(2) and original.excess_pp == pytest.approx(1)
        assert len(storage.attempts(engine, target.key)) == len(storage.completions(engine, target.key, clock())) == 1
        clock.at += timedelta(seconds=35)
        await tick(engine, client, clock(), clock=clock)
        assert storage.assessment(engine, target.key, clock()) == original
    assert wires == ["/api/stock/AAPL/quote", "/api/stock/SPY/quote"]
    with session_factory(engine)() as session:
        assert session.scalar(select(AlfaUwQuota.reserved)) == 2


@pytest.mark.asyncio
async def test_never_mix_legs_across_attempts_and_keep_first_own_partial(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        own = "AAPL" in request.url.path
        ordinal = len(wires) // 2 + 1
        wires.append(request.url.path)
        good = (ordinal == 1 and own) or (ordinal == 2 and not own)
        return httpx.Response(200, json=payload(clock.at, "102" if own else "505")) if good else httpx.Response(404, json={"error": "fixture"})
    async with transport(clock, handler) as client:
        for offset in (2, 32, 62):
            clock.at = T + timedelta(seconds=offset)
            await tick(engine, client, clock(), clock=clock)
            if offset < 62:
                assert storage.assessment(engine, target.key, clock()) is None
    result = storage.assessment(engine, target.key, clock())
    assert result and result.status == "partial" and result.return_pct == pytest.approx(2)
    assert result.excess_pp is None and result.spy_key is None
    assert result.own_key == storage.legs(engine, storage.attempts(engine, target.key)[0].key, clock())[0].key
    assert len(wires) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_auth_and_daily_quota_halt_day_before_spy_or_restarts(capture, status) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(status, json={"error": "daily_request_limit" if status == 429 else "auth"})
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
        assert storage.halted(engine, target.day, clock()) is not None
        clock.at += timedelta(seconds=32)
        await tick(engine, client, clock(), clock=clock)
    assert len(wires) == 1


@pytest.mark.asyncio
async def test_completed_transport_failure_is_recorded_and_next_ordinal_can_run(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        wires.append(request.url.path)
        if len(wires) == 1:
            raise httpx.ReadTimeout("fixture", request=request)
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
        assert len(storage.completions(engine, target.key, clock())) == 1
        assert storage.assessment(engine, target.key, clock()) is None
        clock.at = T + timedelta(seconds=32)
        await tick(engine, client, clock(), clock=clock)
    result = storage.assessment(engine, target.key, clock())
    assert result and result.status == "observed" and len(wires) == 4


@pytest.mark.asyncio
async def test_unknown_claim_waits_without_wire_then_is_unknown_at_deadline(capture) -> None:
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock = CaptureClock()
    assert storage.claim(engine, target, 1, clock())
    def forbidden(request):
        pytest.fail("An incomplete claim must never be replayed")
    async with transport(clock, forbidden) as client:
        await tick(engine, client, clock(), clock=clock)
        assert storage.assessment(engine, target.key, clock()) is None
        clock.at = DEADLINE
        await tick(engine, client, clock(), clock=clock)
    result = storage.assessment(engine, target.key, clock())
    assert result and result.status == "unknown" and result.reason == "incomplete_attempt"
    assert result.return_pct is None


@pytest.mark.asyncio
async def test_disabled_service_never_constructs_clients_engines_or_storage(monkeypatch) -> None:
    from webapp.timed_measurement import service

    monkeypatch.delenv("TIMED_MEASUREMENT_ENABLED", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled service must remain inert")
    monkeypatch.setattr(service, "configured_engine", forbidden)
    await service.loop("sqlite:///:memory:", client_factory=forbidden)
    assert service.safe_status() == {"status": "disabled"}
    service.STATE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_leg", ["own", "spy"])
async def test_physical_return_after_final_confirmation_commit_is_late(capture, monkeypatch, late_leg) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    original = storage.record_leg
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.leg == late_leg:
            clock.at = DEADLINE + timedelta(milliseconds=1)
        return result
    monkeypatch.setattr(storage, "record_leg", delayed)
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
    result = storage.assessment(engine, target.key, clock())
    assert result and result.excess_pp is None
    if late_leg == "own":
        assert result.status == "missing" and result.return_pct is None and len(wires) == 1
    else:
        assert result.status == "partial" and result.return_pct == pytest.approx(2) and len(wires) == 2
    completion = storage.completions(engine, target.key, clock())[0]
    assert (completion.own_confirmed_at if late_leg == "own" else completion.spy_confirmed_at) > DEADLINE


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [None], indirect=True)
async def test_missing_original_entry_spy_stays_partial_and_needs_no_more_quotes(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
        clock.at += timedelta(seconds=35)
        await tick(engine, client, clock(), clock=clock)
    result = storage.assessment(engine, target.key, clock())
    assert result and result.status == "partial" and result.reason == "missing_entry_spy"
    assert result.return_pct == pytest.approx(2) and result.excess_pp is None and len(wires) == 2


@pytest.mark.asyncio
async def test_known_auth_halt_survives_later_packet_record_failure(capture, monkeypatch) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def failed(*args, **kwargs):
        raise RuntimeError("fixture storage failure")
    monkeypatch.setattr(storage, "record_leg", failed)
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(403, json={"error": "auth"})
    async with transport(clock, handler) as client:
        with pytest.raises(RuntimeError, match="fixture storage failure"):
            await tick(engine, client, clock(), clock=clock)
        assert storage.halted(engine, target.day, clock()) is not None
        clock.at += timedelta(seconds=35)
        await tick(engine, client, clock(), clock=clock)
    assert len(wires) == 1


@pytest.mark.asyncio
async def test_local_auth_halt_survives_failed_durable_halt(capture, monkeypatch) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, _target = capture
    clock, wires = CaptureClock(), []
    def failed(*args, **kwargs):
        raise RuntimeError("fixture durable guard failure")
    monkeypatch.setattr(storage, "halt", failed)
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(403, json={"error": "auth"})
    async with transport(clock, handler) as client:
        with pytest.raises(RuntimeError, match="fixture durable guard failure"):
            await tick(engine, client, clock(), clock=clock)
        clock.at += timedelta(seconds=35)
        await tick(engine, client, clock(), clock=clock)
    assert len(wires) == 1


@pytest.mark.asyncio
async def test_burst_cooldown_is_durable_and_ordinal_waits_sixty_seconds(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    def handler(request):
        wires.append(request.url.path)
        if len(wires) == 1:
            return httpx.Response(429, json={"error": "burst"})
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
        assert storage.halted(engine, target.day, clock()) is None
        assert storage.cooling_until(engine, target.day, clock()) == T + timedelta(seconds=62)
        clock.at = T + timedelta(seconds=32)
        await tick(engine, client, clock(), clock=clock)
        assert len(wires) == 1
        clock.at = T + timedelta(seconds=62)
        await tick(engine, client, clock(), clock=clock)
    assert len(wires) == 3 and storage.assessment(engine, target.key, clock()).status == "observed"


def another_target(engine, original):
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.model import digest

    result = original.model_copy(update={"key": digest((original.key, "another")), "primary_key": "second-primary",
        "decision_key": "second-decision", "ticker": "MSFT", "published_at": original.published_at + timedelta(seconds=10),
        "target_at": T + timedelta(seconds=10), "deadline": DEADLINE + timedelta(seconds=10)})
    storage.save_target(engine, result)
    return result


@pytest.mark.asyncio
async def test_restart_holds_other_targets_after_fatal_guard_db_failure(capture, monkeypatch) -> None:
    import httpx
    from webapp.board.db import make_engine
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick
    from webapp.timed_measurement.service import _BootstrapBarrier

    engine, target = capture
    clock, wires = CaptureClock(), []
    real_halt = storage.halt
    def failed(*args, **kwargs):
        raise RuntimeError("fixture lost durable fatal guard")
    monkeypatch.setattr(storage, "halt", failed)
    def fatal(request):
        wires.append(request.url.path)
        return httpx.Response(403, json={"error": "auth"})
    async with transport(clock, fatal) as client:
        with pytest.raises(RuntimeError):
            await tick(engine, client, clock(), clock=clock)
    monkeypatch.setattr(storage, "halt", real_halt)
    another_target(engine, target)
    restarted = make_engine(str(engine.url))
    try:
        clock.at = T + timedelta(seconds=20)
        active = storage.activation(restarted, clock())
        barrier = _BootstrapBarrier(restarted, active, clock())
        assert not barrier.allow(clock()) and barrier.status == "holding"
        def forbidden(request):
            pytest.fail("Restart must hold all new targets while old provider outcome is unknown")
        async with transport(clock, forbidden) as client:
            await tick(restarted, client, clock(), clock=clock, permit_requests=barrier.allow)
            clock.at = DEADLINE + timedelta(milliseconds=1)
            assert not barrier.allow(clock())
            await tick(restarted, client, clock(), clock=clock, permit_requests=barrier.allow)
        assert storage.halted(restarted, target.day, clock()).reason == "unknown_provider_outcome"
        assert len(wires) == 1
    finally:
        restarted.dispose()


@pytest.mark.asyncio
async def test_healthy_old_replica_completion_releases_bootstrap_hold(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick
    from webapp.timed_measurement.service import _BootstrapBarrier

    engine, target = capture
    original = storage.claim(engine, target, 1, T + timedelta(seconds=2))
    assert original
    next_target = another_target(engine, target)
    clock, wires = CaptureClock(T + timedelta(seconds=10)), []
    barrier = _BootstrapBarrier(engine, storage.activation(engine, clock()), clock())
    assert not barrier.allow(clock())
    # A healthy old owner can durably finish a known pre-wire failure. A new
    # worker observes that completion, rather than replaying the original claim.
    storage.complete(engine, original, None, None, clock(), reason="transport", clock=clock)
    assert barrier.allow(clock()) and barrier.status == "ready"
    clock.at = T + timedelta(seconds=12)
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock, permit_requests=barrier.allow)
    assert wires == ["/api/stock/MSFT/quote", "/api/stock/SPY/quote"]
    assert storage.assessment(engine, next_target.key, clock()).status == "observed"
    assert len(storage.attempts(engine, target.key)) == 1


def test_bootstrap_limited_or_failed_ownership_read_never_permits(capture, monkeypatch) -> None:
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.model import OpenAttempts
    from webapp.timed_measurement.service import _BootstrapBarrier

    engine, _target = capture
    clock = CaptureClock()
    barrier = _BootstrapBarrier(engine, storage.activation(engine, clock()), clock())
    real = storage.read_open_attempts
    monkeypatch.setattr(storage, "read_open_attempts", lambda *args: OpenAttempts(rows=(), limited=True))
    assert not barrier.allow(clock()) and barrier.status == "limited"
    def failed(*args):
        raise RuntimeError("fixture ownership read failure")
    monkeypatch.setattr(storage, "read_open_attempts", failed)
    assert not barrier.allow(clock()) and barrier.status == "storage_unavailable"
    monkeypatch.setattr(storage, "read_open_attempts", real)
    assert barrier.allow(clock()) and barrier.status == "ready"


def test_safe_service_status_never_exports_private_fields(monkeypatch) -> None:
    from webapp.timed_measurement import service

    monkeypatch.setattr(service, "STATE", {"status": "ok", "targets": 2, "payload": {"private": "body"},
                                         "database_url": "fixture-private", "at": "aware-utc", "attempts": {"raw": "object"}})
    assert service.safe_status() == {"status": "ok", "targets": 2, "at": "aware-utc"}


def test_preworker_claim_committing_after_initial_snapshot_is_still_held(capture) -> None:
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.service import _BootstrapBarrier

    engine, target = capture
    at = T + timedelta(seconds=10)
    barrier = _BootstrapBarrier(engine, storage.activation(engine, at), at)
    assert barrier.allow(at)
    # The old owner sampled this timestamp before startup; its transaction only
    # becomes visible after our first empty read during a rolling deployment.
    assert storage.claim(engine, target, 1, T + timedelta(seconds=2))
    assert not barrier.allow(at + timedelta(seconds=1))
    assert barrier.status == "holding" and len(barrier.remaining) == 1


@pytest.mark.asyncio
async def test_current_claim_is_owned_even_when_clock_equals_bootstrap_boundary(capture) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick
    from webapp.timed_measurement.service import _BootstrapBarrier

    engine, target = capture
    clock = CaptureClock()
    barrier = _BootstrapBarrier(engine, storage.activation(engine, clock()), clock())
    assert barrier.allow(clock())
    def handler(request):
        return httpx.Response(200, json=payload(clock.at, "505" if "SPY" in request.url.path else "102"))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock, permit_requests=barrier.allow)
    assert storage.assessment(engine, target.key, clock()).status == "observed"


@pytest.mark.asyncio
@pytest.mark.parametrize("capture,own_exit,spy_exit,expected_own,expected_excess", [
    ({"direction": "down"}, "98", "495", 2, 1),
    ({"direction": "down"}, "102", "505", -2, -1),
], indirect=["capture"])
async def test_bearish_actual_collector_keeps_directional_stock_and_spy_signs(
    capture, own_exit, spy_exit, expected_own, expected_excess,
) -> None:
    import httpx
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.collector import tick

    engine, target = capture
    clock, wires = CaptureClock(), []
    assert target.direction == "down" and target.entry_reference == 100 and target.spy_entry == 500
    def handler(request):
        wires.append(request.url.path)
        return httpx.Response(200, json=payload(clock.at, spy_exit if "SPY" in request.url.path else own_exit))
    async with transport(clock, handler) as client:
        await tick(engine, client, clock(), clock=clock)
    result = storage.assessment(engine, target.key, clock())
    assert result and result.status == "observed"
    assert result.return_pct == pytest.approx(expected_own)
    assert result.excess_pp == pytest.approx(expected_excess)
    assert wires == ["/api/stock/AAPL/quote", "/api/stock/SPY/quote"]
