"""Actual source candidates → quota claims → physically receipted financial context."""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, update
from webapp.board import quota_ledger
from webapp.board.db import make_engine
from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE
from webapp.fundamental_context import service, settings, storage, view
from webapp.fundamental_context.transport import FundamentalTransport, Packet
from webapp.live_alpha import store as live
from webapp.news_research import storage as news_store
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow_store

from tests.unit.test_fundamental_context_extract import statements
from tests.unit.test_news_adapter import NOW, news, scan
from tests.unit.test_shadow_bindings import multiple
from tests.unit.test_shadow_collector import Clock
from uoa_detector.calibration import load_profile


@pytest.fixture
def setup(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'fundamentals.db'}")
    live.ensure_live_tables(e)
    news_store.ensure(e)
    shadow_store.ensure(e)
    quota_ledger.ensure_quota_tables(e)
    storage.ensure(e)
    shadow_store.activate(e, lambda: NOW - timedelta(hours=1))
    active = storage.activate(e, lambda: NOW - timedelta(minutes=10))
    clock = Clock(NOW + timedelta(seconds=2))
    env = SimpleNamespace(engine=e, active=active, clock=clock, calls=[], closed=[], status="ok")
    yield env
    service._HALTS.clear()
    service._OWN.clear()
    e.dispose()


def source(env, tickers=("NVDA",)):
    scan(env.engine, data=multiple(tickers))
    news(env.engine)
    assert bindings.discover(env.engine, env.clock(), clock=env.clock)["admitted"] == len(tickers)
    env.clock.at += timedelta(seconds=2)


class Client:
    def __init__(self, env):
        self.env = env

    async def fetch(self, ticker, endpoint, *, before_request=None, deadline=None):
        if before_request:
            await before_request()
        env = self.env
        env.calls.append((ticker, endpoint))
        requested = env.clock()
        if endpoint == "financials":
            payload = statements()
            for group in ("income_statements", "balance_sheets"):
                for row in payload["data"][group]:
                    row["ticker"] = ticker
                    row.pop("updated_at", None)
                    row.pop("inserted_at", None)
        else:
            payload = {
                "data": [
                    dict(
                        ticker=ticker,
                        fiscal_date_ending="2026-09-30",
                        report_type="quarterly",
                        reported_eps="2",
                        estimated_eps="1",
                    )
                ]
            }
        raw = json.dumps(payload).encode()
        status = env.status
        return Packet(
            ticker,
            status,
            requested,
            env.clock(),
            payload,
            401 if status == "auth" else 200,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            True,
            None,
            None,
            raw,
            endpoint,
        )

    async def aclose(self):
        self.env.closed.append(True)


def run(env):
    async def pause(seconds):
        env.clock.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    return asyncio.run(
        service.tick(
            env.engine,
            env.clock(),
            client_factory=lambda: Client(env),
            clock=env.clock,
            sleep=pause,
        )
    )


def test_real_source_eight_requests_bound_four_candidates_and_restart_never_refetches(setup):
    e = setup.engine
    source(setup, ("AAPL", "MSFT", "NVDA", "TSLA"))
    result = run(setup)
    assert result["status"] == "active" and result["reserved_requests"] == result["packets"] == 8
    assert len(setup.calls) == len(set(setup.calls)) == 8 and len(setup.closed) == 4
    assert quota_ledger.reserved_on(e, NOW.date()) == 8
    run(setup)
    assert len(setup.calls) == 8
    report = view.build(e, setup.clock())
    assert (
        report.status == "RECORDED" and report.candidate_count == 4 and report.invalid_records == 0
    )
    assert all(len(row.facts) == 2 for row in report.rows)
    assert all(
        next(m for m in row.facts[0].metrics if m.name == "revenue").value == 120
        for row in report.rows
    )
    assert all(
        next(m for m in row.facts[1].metrics if m.name == "eps_surprise").value is None
        for row in report.rows
    )
    assert all(
        f.available_at >= setup.active.confirmed_at for row in report.rows for f in row.facts
    )


def test_concurrent_ticks_do_not_duplicate_or_replay_request_slots(setup):
    source(setup, ("AAPL", "MSFT", "NVDA", "TSLA"))

    async def pause(seconds):
        setup.clock.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def both():
        return await asyncio.gather(
            *(
                service.tick(
                    setup.engine,
                    setup.clock(),
                    client_factory=lambda: Client(setup),
                    clock=setup.clock,
                    sleep=pause,
                )
                for _ in range(2)
            )
        )

    asyncio.run(both())
    assert len(setup.calls) == len(set(setup.calls)) == 8
    assert quota_ledger.reserved_on(setup.engine, NOW.date()) == 8


def test_pre_activation_and_stale_sources_are_not_replayed(setup):
    source(setup)
    setup.clock.at = NOW + timedelta(seconds=63)
    assert run(setup)["status"] == "waiting" and not setup.calls
    assert storage.count(setup.engine, "claim", setup.clock(), day=NOW.date()) == 0


def test_read_projection_has_no_writes_and_keeps_raw_packets_private(setup):
    source(setup)
    run(setup)
    writes = []
    event.listen(
        setup.engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
            else None
        ),
    )
    r = view.build(setup.engine, setup.clock())
    assert r.candidate_count == 1 and not writes
    assert all(not hasattr(o, "blob") for row in r.rows for o in row.observations)
    assert r.activation_at == setup.active.confirmed_at


def test_missing_storage_get_does_not_create_tables(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    writes = []
    event.listen(
        e,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql) if sql.lstrip().upper().startswith(("CREATE", "INSERT")) else None
        ),
    )
    r = view.build(e, NOW)
    assert r.status == "STORAGE_UNAVAILABLE" and not writes
    e.dispose()


def test_shared_cap_refusal_halts_without_wire(setup):
    source(setup)
    quota_ledger.reserve(setup.engine, day=NOW.date(), n=24000, cap=24000, now=setup.clock())
    result = run(setup)
    assert result["status"] == "halted" and not setup.calls
    assert service.halted(setup.engine, NOW.date(), setup.clock()) == "shared_quota_limit"


@pytest.mark.parametrize("status", ["auth", "daily_quota", "quota_unknown"])
def test_known_auth_or_quota_failure_halts_future_requests(setup, status):
    source(setup)
    setup.status = status
    assert run(setup)["status"] == "halted" and len(setup.calls) == 1
    run(setup)
    assert len(setup.calls) == 1


def test_orphan_ownership_holds_then_halts_and_never_replays(setup):
    source(setup)
    storage.put(
        setup.engine,
        key="orphan",
        kind="claim",
        at=setup.clock(),
        day=NOW.date(),
        payload={"identity": settings.identity()},
        clock=setup.clock,
    )
    assert run(setup)["status"] == "ownership_pending" and not setup.calls
    setup.clock.at += timedelta(seconds=31)
    assert run(setup)["status"] == "halted" and not setup.calls


def test_future_or_corrupt_ownership_cannot_hide_from_bootstrap(setup):
    source(setup)
    future = setup.clock() + timedelta(seconds=10)
    storage.put(
        setup.engine,
        key="future",
        kind="claim",
        at=future,
        day=NOW.date(),
        payload={},
        clock=lambda: future,
    )
    assert run(setup)["status"] == "halted" and not setup.calls


def test_corrupted_raw_packet_cannot_supply_financial_numbers(setup):
    source(setup)
    run(setup)
    packet = storage.list_events(setup.engine, "packet", setup.clock(), day=NOW.date())[0]
    with setup.engine.begin() as conn:
        conn.execute(
            update(storage.FundamentalEvent)
            .where(storage.FundamentalEvent.key == packet.key)
            .values(blob=b"corrupt")
        )
    r = view.build(setup.engine, setup.clock())
    assert any(
        o.status == "unknown" and o.facts is None for row in r.rows for o in row.observations
    )


def test_unique_request_and_slot_ownership_is_atomic_under_threads(setup):
    def attempt(_):
        return storage.put(
            setup.engine,
            key="one",
            kind="claim",
            at=NOW,
            day=NOW.date(),
            unique_owner="same",
            payload={},
            clock=lambda: NOW,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(attempt, range(4)))
    assert sum(row is not None for row in rows) == 1


def test_dual_postcommit_availability_and_unconfirmed_packets_cannot_be_repaired(
    setup, monkeypatch
):
    calls = []
    original = storage._insert

    def insert(engine, model, values):
        result = original(engine, model, values)
        if model is storage.FundamentalReceipt and values["stage"] == "recorded":
            setup.clock.at += timedelta(seconds=30)
        calls.append(model.__name__)
        return result

    monkeypatch.setattr(storage, "_insert", insert)
    p = storage.put(
        setup.engine, key="late", kind="packet", at=setup.clock(), payload={}, clock=setup.clock
    )
    assert p.confirmed_at == NOW + timedelta(seconds=32)
    assert calls == ["FundamentalEvent", "FundamentalReceipt", "FundamentalReceipt"]
    assert (
        storage.put(
            setup.engine, key="late", kind="packet", at=setup.clock(), payload={}, clock=setup.clock
        )
        is None
    )


def test_disabled_loop_never_opens_database_or_client(monkeypatch):
    monkeypatch.setenv("FUNDAMENTAL_CONTEXT_ENABLED", "false")
    monkeypatch.setattr(service, "configured_engine", lambda *_: pytest.fail("disabled DB"))
    asyncio.run(service.loop("unused", client_factory=lambda: pytest.fail("disabled provider")))


def test_native_pool_has_independent_small_pool_and_timeouts(monkeypatch):
    seen = []
    marker = object()
    monkeypatch.setattr(service, "create_engine", lambda *a, **k: seen.append(k) or marker)
    assert service.configured_engine("postgresql://x:y@localhost/db") is marker
    assert seen[0]["pool_size"] == 2 and seen[0]["max_overflow"] == 0
    assert "statement_timeout=2000" in seen[0]["connect_args"]["options"]


@pytest.mark.parametrize(
    "response_status,body,expected",
    [
        (200, {"data": {"income_statements": []}}, "ok"),
        (200, {"data": []}, "ok"),
        (401, {"error": "bad"}, "auth"),
        (403, {"error": "entitlement"}, "auth"),
        (429, {"code": "daily_request_limit"}, "daily_quota"),
    ],
)
def test_native_single_attempt_transport_fixed_paths_raw_bytes_and_status(
    response_status, body, expected
):
    seen = []

    def reply(request):
        seen.append(request.url.path)
        return httpx.Response(
            response_status,
            json=body,
            headers={"x-uw-daily-req-count": "1", "x-uw-token-req-limit": "30000"},
        )

    transport = FundamentalTransport(
        api_key=SecretStr("fixture"),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=8,
        max_response_bytes=1048576,
        clock=lambda: NOW,
        transport=httpx.MockTransport(reply),
    )

    async def get():
        try:
            return await transport.fetch("AAPL", "financials")
        finally:
            await transport.aclose()

    packet = asyncio.run(get())
    assert seen == ["/api/stock/AAPL/financials"] and packet.status == expected
    assert (
        packet.raw_body is not None
        and hashlib.sha256(packet.raw_body).hexdigest() == packet.body_sha256
    )


def test_current_but_pre_activation_candidate_cannot_trigger_new_context(setup, monkeypatch):
    source(setup)
    original = settings.identity()
    monkeypatch.setattr(
        settings, "identity", lambda: {**original, "implementation_hash": "new-implementation"}
    )
    active = storage.activate(setup.engine, setup.clock)
    assert active.confirmed_at == setup.clock()
    assert run(setup)["status"] == "waiting" and not setup.calls


def test_wire_timeout_unknown_outcome_halts_after_one_claim_without_replay(setup):
    source(setup)

    class TimeoutClient(Client):
        async def fetch(self, ticker, endpoint, *, before_request=None, deadline=None):
            if before_request:
                await before_request()
            setup.calls.append((ticker, endpoint))
            return Packet(
                ticker,
                "timeout",
                setup.clock(),
                None,
                None,
                None,
                None,
                0,
                True,
                None,
                None,
                endpoint=endpoint,
            )

    async def run_tick():
        return await service.tick(
            setup.engine,
            setup.clock(),
            client_factory=lambda: TimeoutClient(setup),
            clock=setup.clock,
        )

    assert asyncio.run(run_tick())["status"] == "halted" and len(setup.calls) == 1
    assert asyncio.run(run_tick())["status"] == "halted" and len(setup.calls) == 1
    assert view.build(setup.engine, setup.clock()).halt_reason == "unknown_provider_outcome"


def test_native_body_limit_never_exposes_unbounded_packet():
    seen = []

    def reply(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=b"x" * 200, headers={"content-length": "200"})

    t = FundamentalTransport(
        api_key=SecretStr("fixture"),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=8,
        max_response_bytes=100,
        clock=lambda: NOW,
        transport=httpx.MockTransport(reply),
    )

    async def get():
        try:
            return await t.fetch("AAPL", "financials")
        finally:
            await t.aclose()

    p = asyncio.run(get())
    assert p.status == "body_limit" and p.raw_body is None and len(seen) == 1


def test_before_wire_quota_refusal_never_reaches_native_transport():
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {}})

    t = FundamentalTransport(
        api_key=SecretStr("fixture"),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=8,
        max_response_bytes=100,
        clock=lambda: NOW,
        transport=httpx.MockTransport(reply),
    )

    async def refuse():
        raise ValueError("refused")

    async def get():
        try:
            await t.fetch("AAPL", "financials", before_request=refuse)
        finally:
            await t.aclose()

    with pytest.raises(ValueError, match="refused"):
        asyncio.run(get())
    assert not seen
