"""Actual durable pair capture, explicit retries and refusal-safe ownership.

These tests isolate collection after source admission; bindings tests independently
exercise actual source scans. Quotes still pass the real raw-wire parser/store.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables, reserved_on
from webapp.shadow_capture import bindings, collector, config, storage
from webapp.timed_measurement.transport import Packet

T = datetime(2026, 11, 4, 15, tzinfo=UTC)


class Clock:
    def __init__(self, at=T):
        self.at = at

    def __call__(self):
        return self.at


class Client:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.prices = {"NVDA": 100.0, "SPY": 500.0}
        self.damage = None
        self.last_daily_request_count = 0
        self.last_daily_request_at = None

    async def fetch(self, ticker, *, before_request=None, deadline=None):
        if before_request is not None:
            await before_request()
        at = self.clock()
        self.calls.append(ticker)
        self.last_daily_request_count += 1
        self.last_daily_request_at = at
        body = {
            "data": {
                "ticker": ticker,
                "market_time": "regular",
                "last_trade": {"price": self.prices[ticker], "time": int(at.timestamp() * 1000)},
            }
        }
        raw = json.dumps(body).encode()
        packet = Packet(
            ticker,
            "ok",
            at,
            at,
            body,
            200,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            True,
            self.last_daily_request_count,
            at,
            raw,
        )
        return self.damage(packet, len(self.calls)) if self.damage else packet

    async def aclose(self):
        return None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path / 'collect.db'}")
    storage.ensure(e)
    ensure_quota_tables(e)
    # Actual source binding has separate tests; this fixture models its admitted
    # immutable output while exercising real storage, packet admission and quota.
    monkeypatch.setattr(bindings, "valid_candidate", lambda *_: True)
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    clock = Clock(T - timedelta(seconds=30))
    active = storage.activate(e, clock)
    clock.at = T
    candidate = storage.put(
        e,
        key=bindings.candidate_key(T.date(), 1),
        kind="candidate",
        at=T,
        day=T.date(),
        parent_key=active.key,
        unique_owner=bindings.candidate_owner(T.date(), "NVDA"),
        payload={
            "identity": config.identity(),
            "activation_key": active.key,
            "slot": 1,
            "ticker": "NVDA",
            "scan_id": "source",
            "snapshot_hash": "a" * 64,
            "scan_finished_at": T.isoformat(),
            "rs": True,
            "news": False,
            "base": True,
        },
        clock=clock,
    )
    assert candidate is not None
    yield SimpleNamespace(
        engine=e, clock=clock, active=active, candidate=candidate, client=Client(clock)
    )
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    e.dispose()


def run(fixture, seconds):
    fixture.clock.at = T + timedelta(seconds=seconds)
    return asyncio.run(
        collector.tick(fixture.engine, fixture.client, fixture.clock(), clock=fixture.clock)
    )


def target(fixture, phase="entry"):
    return storage.get(
        fixture.engine, collector._key("target", fixture.candidate.key, phase), fixture.clock()
    )


def outcome(fixture, phase="entry"):
    return collector.verified_outcome(fixture.engine, target(fixture, phase), fixture.clock())


def changed(packet, **values):
    from dataclasses import replace

    return replace(packet, **values)


def raw_changed(packet, change):
    body = json.loads(packet.raw_body)
    change(body)
    raw = json.dumps(body).encode()
    return changed(
        packet,
        payload=body,
        raw_body=raw,
        body_sha256=hashlib.sha256(raw).hexdigest(),
        body_bytes=len(raw),
    )


def test_real_raw_pair_entry_and_fifteen_sixty_follow_entry_confirmation(setup):
    f = setup
    assert run(f, 0)["attempts"] == 0
    assert target(f).payload["target_at"] == (T + timedelta(seconds=60)).isoformat()
    assert run(f, 61)["attempts"] == 0
    assert run(f, 62)["observed"] == 1
    entry = outcome(f)
    assert entry and entry.payload["own_price"] == 100 and entry.payload["spy_price"] == 500
    assert reserved_on(f.engine, T.date()) == 2
    assert run(f, 63)["attempts"] == 0
    for phase, seconds, own, spy in [("m15", 964, 102, 501), ("m60", 3664, 103, 502)]:
        f.client.prices = {"NVDA": own, "SPY": spy}
        assert run(f, seconds)["observed"] == 1
        value = outcome(f, phase)
        assert value and value.payload["own_price"] == own and value.payload["spy_price"] == spy
        assert (
            target(f, phase).payload["target_at"]
            == (entry.confirmed_at + timedelta(minutes=15 if phase == "m15" else 60)).isoformat()
        )
    assert len(f.client.calls) == 6 and reserved_on(f.engine, T.date()) == 6
    assert run(f, 3665)["attempts"] == 0 and len(f.client.calls) == 6


def test_quote_payload_is_redecoded_from_original_recorded_wire_not_client_dict(setup):
    f = setup
    f.client.damage = lambda p, _n: changed(p, payload={"data": {"last_trade": {"price": 999999}}})
    run(f, 62)
    assert outcome(f).payload["own_price"] == 100


@pytest.mark.parametrize(
    "damage", ["ticker", "phase", "seconds", "future", "before", "price", "hash", "raw"]
)
def test_bad_native_quote_never_becomes_an_observed_pair(setup, damage):
    f = setup

    def alter(p, n):
        if n != 1:
            return p
        if damage == "hash":
            return changed(p, body_sha256="0" * 64)
        if damage == "raw":
            return changed(
                p, raw_body=b"{bad", body_bytes=4, body_sha256=hashlib.sha256(b"{bad").hexdigest()
            )

        def body(b):
            data = b["data"]
            if damage == "ticker":
                data["ticker"] = "OTHER"
            elif damage == "phase":
                data["market_time"] = "premarket"
            elif damage == "seconds":
                data["last_trade"]["time"] = int(f.clock().timestamp())
            elif damage == "future":
                data["last_trade"]["time"] += 1000
            elif damage == "before":
                data["last_trade"]["time"] -= 3000
            else:
                data["last_trade"]["price"] = -1

        return raw_changed(p, body)

    f.client.damage = alter
    run(f, 62)
    assert outcome(f) is None
    assert len(f.client.calls) == 2


def test_retry_spacing_and_no_cross_attempt_leg_mixing(setup):
    f = setup

    def alternate(p, n):
        if n in (2, 3):
            return raw_changed(p, lambda b: b["data"].update(market_time="premarket"))
        return p

    f.client.damage = alternate
    run(f, 62)
    assert outcome(f) is None
    assert run(f, 91)["attempts"] == 0
    run(f, 92)
    assert outcome(f) is None and len(f.client.calls) == 4
    run(f, 122)
    assert outcome(f) and outcome(f).payload["attempt_key"] == collector._key(
        "attempt", target(f).key, 3
    )
    assert len(f.client.calls) == 6


def test_phase_exhaustion_is_unknown_and_not_zero_return(setup):
    f = setup
    f.client.damage = lambda p, _: raw_changed(
        p, lambda b: b["data"].update(market_time="premarket")
    )
    for second in (62, 92, 122):
        run(f, second)
    run(f, 123)
    value = outcome(f)
    assert value.payload["status"] == "missing" and value.payload["reason"] == "attempts_exhausted"
    assert value.payload["own_price"] is value.payload["spy_price"] is None


@pytest.mark.parametrize("status", ["auth", "daily_quota", "quota_unknown", "timeout"])
def test_refusal_and_unknown_wire_outcome_stop_day_before_spy(setup, status):
    f = setup
    f.client.damage = lambda p, _: changed(
        p,
        status=status,
        http_status=None if status == "timeout" else 401,
        raw_body=None if status == "timeout" else p.raw_body,
    )
    run(f, 62)
    assert len(f.client.calls) == 1
    run(f, 93)
    assert len(f.client.calls) == 1 and outcome(f).payload["status"] == "missing"
    collector._HALTS.clear()
    run(f, 124)
    assert len(f.client.calls) == 1


def test_rate_limit_cooldown_then_new_completed_attempt(setup):
    f = setup
    f.client.damage = lambda p, n: (
        changed(p, status="rate_limited", http_status=429) if n == 1 else p
    )
    run(f, 62)
    assert len(f.client.calls) == 1
    run(f, 121)
    assert len(f.client.calls) == 1
    run(f, 122)
    assert len(f.client.calls) == 3 and outcome(f)


def test_shared_quota_refusal_never_reaches_wire(setup, monkeypatch):
    f = setup
    monkeypatch.setattr(
        collector,
        "load_board_settings",
        lambda: SimpleNamespace(refresh=SimpleNamespace(daily_request_soft_cap=0)),
    )
    run(f, 62)
    assert f.client.calls == []
    run(f, 93)
    assert outcome(f).payload["status"] == "missing" and f.client.calls == []


def test_orphan_attempt_blocks_restart_then_halts_on_deadline(setup):
    f = setup
    run(f, 0)
    t = target(f)
    at = T + timedelta(seconds=62)
    f.clock.at = at
    storage.put(
        f.engine,
        key=collector._key("attempt", t.key, 1),
        kind="attempt",
        at=at,
        day=T.date(),
        parent_key=t.key,
        payload={"identity": config.identity(), "ordinal": 1, "deadline": t.payload["deadline"]},
        clock=f.clock,
    )
    assert run(f, 63)["status"] == "bootstrap_hold" and f.client.calls == []
    collector._HALTS.clear()
    assert run(f, 360)["status"] == "halted" and f.client.calls == []


def test_storage_failure_after_native_refusal_retains_memory_guard(setup, monkeypatch):
    f = setup
    f.client.damage = lambda p, _: changed(p, status="auth", http_status=401)
    original = storage.put

    def fail(*args, **kwargs):
        if kwargs["kind"] == "packet":
            raise RuntimeError("synthetic_storage_failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "put", fail)
        with pytest.raises(RuntimeError):
            run(f, 62)
    assert len(f.client.calls) == 1
    run(f, 93)
    assert len(f.client.calls) == 1


def test_raw_database_tamper_invalidates_observed_pair(setup):
    f = setup
    run(f, 62)
    value = outcome(f)
    assert value
    with f.engine.begin() as c:
        c.execute(
            update(storage.ShadowEvent)
            .where(storage.ShadowEvent.key == value.payload["own_packet_key"])
            .values(blob=b"broken")
        )
    assert outcome(f) is None


def test_unverified_parent_cannot_schedule_or_capture(setup, monkeypatch):
    f = setup
    monkeypatch.setattr(bindings, "valid_candidate", lambda *_: False)
    assert run(f, 62)["targets"] == 0 and f.client.calls == []


def test_clock_cannot_rewind_between_claim_and_wire(setup):
    f = setup
    run(f, 0)
    f.clock.at = T + timedelta(seconds=61)
    with pytest.raises(collector._RefusedError):
        asyncio.run(collector.tick(f.engine, f.client, T + timedelta(seconds=62), clock=f.clock))
    assert f.client.calls == []


def test_module_quota_caps_global_slots_before_shared_spend_or_wire(setup):
    f = setup
    for slot in range(1, config.settings().module_daily_cap + 1):
        storage.put(
            f.engine,
            key=collector._key("quota_slot", T.date(), slot),
            kind="quota_slot",
            at=T,
            day=T.date(),
            payload={"slot": slot},
            clock=f.clock,
        )
    run(f, 62)
    assert f.client.calls == [] and reserved_on(f.engine, T.date()) == 0
    run(f, 93)
    assert outcome(f).payload["status"] == "missing" and f.client.calls == []


def test_source_is_revalidated_at_send_even_when_target_was_scheduled_earlier(setup, monkeypatch):
    f = setup
    run(f, 0)
    assert target(f)
    monkeypatch.setattr(bindings, "valid_candidate", lambda *_: False)
    assert run(f, 62)["status"] == "halted" and f.client.calls == []


def test_same_attempt_pair_skew_limit_is_not_a_cross_timestamp_benchmark(setup):
    f = setup
    original = f.client.fetch

    async def delayed(ticker, **kwargs):
        if ticker == "SPY":
            f.clock.at += timedelta(seconds=61)
        return await original(ticker, **kwargs)

    f.client.fetch = delayed
    run(f, 62)
    assert len(f.client.calls) == 2 and outcome(f) is None


@pytest.mark.parametrize("kind", ["packet", "outcome"])
def test_post_commit_confirmation_after_deadline_is_never_observed(setup, monkeypatch, kind):
    f = setup
    original = storage.put

    def late(*args, **kwargs):
        if kwargs["kind"] == kind:
            f.clock.at = T + timedelta(seconds=361)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "put", late)
        result = run(f, 62)
    assert result["observed"] == 0 and outcome(f) is None
    run(f, 362)
    assert len(f.client.calls) <= 2


def test_deadline_crossed_after_quota_reservation_does_not_send(setup, monkeypatch):
    f = setup
    original = collector.quota_ledger.reserve

    def cross(*args, **kwargs):
        got = original(*args, **kwargs)
        f.clock.at = T + timedelta(seconds=361)
        return got

    monkeypatch.setattr(collector.quota_ledger, "reserve", cross)
    result = run(f, 62)
    assert result["observed"] == 0 and f.client.calls == []
    assert reserved_on(f.engine, T.date()) == 1


def test_concurrent_ticks_claim_only_one_physical_attempt(setup):
    f = setup
    run(f, 0)
    original = f.client.fetch

    async def interleave(ticker, **kwargs):
        await asyncio.sleep(0)
        return await original(ticker, **kwargs)

    f.client.fetch = interleave
    f.clock.at = T + timedelta(seconds=62)

    async def collect():
        return await asyncio.gather(
            *(collector.tick(f.engine, f.client, f.clock(), clock=f.clock) for _ in range(2))
        )

    results = asyncio.run(collect())
    assert sum(r["observed"] for r in results) == 1 and len(f.client.calls) == 2
    assert reserved_on(f.engine, T.date()) == 2


def test_historical_verified_pair_read_does_not_write_or_fetch(setup):
    from sqlalchemy import event

    f = setup
    run(f, 62)
    writes = []

    def inspect_sql(_c, _cur, sql, *_args):
        if (
            sql.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
        ):
            writes.append(sql)

    event.listen(f.engine, "before_cursor_execute", inspect_sql)
    before = len(f.client.calls)
    assert outcome(f)
    assert writes == [] and len(f.client.calls) == before


def test_future_attempt_ownership_after_clock_rollback_blocks_all_wire(setup):
    f = setup
    run(f, 0)
    t = target(f)
    f.clock.at = T + timedelta(seconds=100)
    storage.put(
        f.engine,
        key=collector._key("attempt", t.key, 1),
        kind="attempt",
        at=f.clock(),
        day=T.date(),
        parent_key=t.key,
        payload={"identity": config.identity(), "ordinal": 1, "deadline": t.payload["deadline"]},
        clock=f.clock,
    )
    result = run(f, 62)
    assert result["status"] == "halted" and f.client.calls == []


def test_wrong_parent_completion_cannot_hide_unknown_attempt(setup):
    f = setup
    run(f, 0)
    t = target(f)
    f.clock.at = T + timedelta(seconds=62)
    attempt = storage.put(
        f.engine,
        key=collector._key("attempt", t.key, 1),
        kind="attempt",
        at=f.clock(),
        day=T.date(),
        parent_key=t.key,
        payload={"identity": config.identity(), "ordinal": 1, "deadline": t.payload["deadline"]},
        clock=f.clock,
    )
    storage.put(
        f.engine,
        key=collector._key("attempt_done", attempt.key),
        kind="attempt_done",
        at=f.clock(),
        day=T.date(),
        parent_key="another-attempt",
        payload={"identity": config.identity(), "paired": False},
        clock=f.clock,
    )
    assert run(f, 63)["status"] == "bootstrap_hold" and f.client.calls == []


def test_database_failure_before_attempt_retains_engine_memory_guard(setup, monkeypatch):
    f = setup
    with monkeypatch.context() as patch:
        patch.setattr(storage, "active", lambda *_: (_ for _ in ()).throw(RuntimeError("db")))
        with pytest.raises(RuntimeError):
            run(f, 62)
    assert run(f, 63)["status"] == "halted" and f.client.calls == []
