"""Prospective ownership, clocks and conservative budgets survive worker restart."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine
from webapp.board.db import make_engine, session_factory
from webapp.board.quota_ledger import reserved_on
from webapp.timed_measurement import storage
from webapp.timed_measurement.config import settings
from webapp.timed_measurement.model import Assessment, Attempt, LegInput, Target, canonical, digest

T = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    value = make_engine(f"sqlite:///{tmp_path / 'timed.db'}")
    storage.ensure(value)
    return value


def target(engine: Engine, *, suffix: str = "one", **changes: object) -> Target:
    active = storage.activate(engine, lambda: T)
    published = T+timedelta(minutes=45)
    values: dict[str, object] = dict(key=digest(("target", suffix)), activation_key=active.key, profile_hash=active.profile_hash,
        opening_profile_hash=active.opening_profile_hash, primary_key=f"primary-{suffix}", decision_key=f"decision-{suffix}",
        ticker="AAPL", day=published.date(), direction="up", instrument="stock", published_at=published,
        entry_reference=100, entry_source_at=published-timedelta(seconds=1), spy_entry=500,
        spy_entry_source_at=published-timedelta(seconds=1), source_scan_id="scan", source_snapshot_hash="a"*64,
        source_card_hash="b"*64, horizon="m15", horizon_minutes=15, target_at=published+timedelta(minutes=15),
        deadline=published+timedelta(minutes=20), eligible=True, reason="", discovered_at=published+timedelta(seconds=1))
    values.update(changes)
    value = Target.model_validate(values)
    storage.save_target(engine, value)
    return value


def attempt(engine: Engine, value: Target) -> Attempt:
    result = storage.claim(engine, value, 1, value.target_at+timedelta(seconds=2))
    assert result is not None
    return result


def own(engine: Engine, claimed: Attempt, *, status: str = "ok", received: bool = True):
    at = claimed.claimed_at
    reservation = storage.reserve_leg(engine, claimed, "own", "AAPL", at, 100, shared_cap=24000)
    assert reservation.granted
    raw = canonical({"data": {"last_trade": {"price": "102", "time": int((at+timedelta(seconds=1)).timestamp()*1000)}}})
    return storage.record_leg(engine, claimed, LegInput(leg="own", ticker="AAPL", status=status,
        requested_at=at+timedelta(seconds=1), received_at=at+timedelta(seconds=2) if received else None,
        payload=None, raw_body=raw if received else None, wire_body_hash=hashlib.sha256(raw).hexdigest() if received else None,
        body_bytes=len(raw) if received else 0, wire_attempted=True, http_status=200 if received else None,
        price=102 if status == "ok" else None, trade_at=at+timedelta(seconds=1) if status == "ok" else None,
        market_phase="regular", source_path="/api/stock/AAPL/quote"), lambda: at+timedelta(seconds=3))


def test_activation_restarts_preserve_first_confirmed_boundary(engine: Engine) -> None:
    first = storage.activate(engine, lambda: T)
    again = storage.activate(engine, lambda: T+timedelta(days=1))
    assert first == again
    assert storage.activation(engine, T-timedelta(seconds=1)) is None
    assert storage.activation(engine, T) == first
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(storage.TimedActivation)) == 1
        assert session.scalar(select(func.count()).select_from(storage.TimedReceipt)) == 2


def test_missing_or_corrupt_confirmation_never_activates(engine: Engine) -> None:
    storage.activate(engine, lambda: T)
    with engine.begin() as connection:
        connection.execute(update(storage.TimedReceipt).where(storage.TimedReceipt.stage == "confirmed").values(at=T-timedelta(days=1)))
    assert storage.activation(engine, T+timedelta(days=1)) is None


def test_current_epoch_asof_and_first_target_ownership_are_bounded(engine: Engine) -> None:
    current = target(engine)
    other = target(engine, suffix="other", activation_key="old-method", profile_hash="old-profile")
    assert storage.targets(engine, current.discovered_at-timedelta(seconds=1)) == ()
    assert storage.targets(engine, current.discovered_at) == (current,)
    assert storage.target_count(engine, current.discovered_at) == 1
    assert storage.claim(engine, other, 1, other.target_at+timedelta(seconds=2)) is None
    before = target(engine, suffix="before", published_at=T-timedelta(seconds=1))
    assert storage.claim(engine, before, 1, before.target_at+timedelta(seconds=2)) is None


def test_replicas_claim_once_and_restart_cannot_replay(engine: Engine) -> None:
    value = target(engine)
    at = value.target_at+timedelta(seconds=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: storage.claim(engine, value, 1, at), range(2)))
    assert sum(c is not None for c in claims) == 1
    assert storage.claim(engine, value, 1, at+timedelta(seconds=1)) is None
    assert storage.claim(engine, value, 2, at+timedelta(seconds=60)) is None


def test_next_attempt_requires_durable_completion_and_actual_spacing(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    completed = storage.complete(engine, first, None, None, first.claimed_at+timedelta(seconds=1), "transport_failed",
                                 clock=lambda: first.claimed_at+timedelta(seconds=3))
    assert storage.claim(engine, value, 2, completed.confirmed_at+timedelta(seconds=29)) is None
    assert storage.claim(engine, value, 2, completed.confirmed_at+timedelta(seconds=30)) is not None


def test_module_and_shared_request_reservations_are_atomic(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    refused = storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, observed=24000, shared_cap=24000)
    assert not refused.granted and refused.reason == "shared_cap"
    assert reserved_on(engine, value.day) == 24000  # Native knowledge must survive refusal.
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(storage.TimedRequestSlot)) == 0


def test_unknown_native_usage_does_not_manufacture_zero_floor(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    refused = storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, shared_cap=24000)
    assert not refused.granted and refused.reason == "quota_unknown"
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(storage.TimedRequestSlot)) == 0


def test_duplicate_leg_and_wrong_claim_ticker_do_not_spend_again(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    got = storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, 100, shared_cap=24000)
    repeat = storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, 100, shared_cap=24000)
    foreign = storage.reserve_leg(engine, first, "spy", "MSFT", first.claimed_at, 100, shared_cap=24000)
    assert got.granted and not repeat.granted and not foreign.granted
    assert reserved_on(engine, value.day) == 101


def test_module_cap_is_enforced_before_an_additional_request(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    with engine.begin() as connection:
        connection.execute(storage.TimedRequestSlot.__table__.insert(), [dict(key=f"fixture-{n}", profile_hash=value.profile_hash,
            day=value.day, ordinal=n, attempt_key=f"historical-{n}", leg="own", ticker="AAPL", reserved_at=first.claimed_at)
            for n in range(1, settings().module_daily_cap+1)])
    result = storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, 100, shared_cap=24000)
    assert not result.granted and result.reason == "module_cap"
    assert reserved_on(engine, value.day) == 100


def test_packet_postcommit_order_raw_hash_and_no_future_receipt(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    recorded = own(engine, first)
    assert recorded.requested_at < recorded.received_at <= recorded.receipt_at <= recorded.confirmed_at
    assert storage.evidence(engine, recorded.key, recorded.confirmed_at-timedelta(microseconds=1)) is None
    assert storage.evidence(engine, recorded.key, recorded.confirmed_at) == recorded
    with engine.begin() as connection:
        connection.execute(update(storage.TimedPacket).where(storage.TimedPacket.key == recorded.key).values(compressed_body=b"corrupt"))
    assert storage.evidence(engine, recorded.key, recorded.confirmed_at) is None


def test_transport_timeout_is_durable_without_invented_response_clock(engine: Engine) -> None:
    first = attempt(engine, target(engine))
    recorded = own(engine, first, status="timeout", received=False)
    assert recorded.wire_attempted and recorded.requested_at is not None
    assert recorded.received_at is None and recorded.price is None and recorded.trade_at is None
    result = storage.complete(engine, first, recorded, None, recorded.confirmed_at, "timeout",
                              own_confirmed_at=recorded.confirmed_at, clock=lambda: recorded.confirmed_at)
    assert storage.completions(engine, first.target_key, result.confirmed_at) == (result,)


def test_postcommit_time_advances_when_commit_is_slow(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    assert storage.reserve_leg(engine, first, "own", "AAPL", first.claimed_at, 100, shared_cap=24000).granted
    moment = [value.deadline-timedelta(seconds=1)]
    def committed(connection: object) -> None:
        moment[0] += timedelta(seconds=2)
    event.listen(engine, "commit", committed)
    packet = LegInput(leg="own", ticker="AAPL", status="ok", wire_attempted=True, price=102,
        requested_at=value.deadline-timedelta(seconds=2), received_at=value.deadline-timedelta(seconds=1),
        trade_at=value.deadline-timedelta(seconds=2), payload={"valid": True}, source_path="/api/stock/AAPL/quote")
    recorded = storage.record_leg(engine, first, packet, lambda: moment[0])
    assert recorded.received_at < value.deadline < recorded.confirmed_at
    assert moment[0] > recorded.confirmed_at  # Final confirmation write can lag; collector must recheck physical return time.


def test_completion_preserves_later_physical_per_leg_confirmation(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    recorded = own(engine, first)
    later = value.deadline+timedelta(seconds=1)
    result = storage.complete(engine, first, recorded, None, later, "recording_late", own_confirmed_at=later, clock=lambda: later)
    again = storage.completions(engine, value.key, later)[0]
    assert again == result and again.own_confirmed_at == later


def test_assessment_is_immutable_and_empty_is_not_zero(engine: Engine) -> None:
    value = target(engine)
    assessment = Assessment(key=value.key, target_key=value.key, assessed_at=value.deadline, status="missing", reason="no_attempt")
    assert storage.assess(engine, assessment)
    assert not storage.assess(engine, assessment.model_copy(update={"reason": "replacement"}))
    assert storage.assessment(engine, value.key, value.deadline) == assessment
    assert assessment.return_pct is None and assessment.excess_pp is None


def test_halt_and_cooldown_are_durable_and_block_new_reservations(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    at = first.claimed_at
    storage.cooldown(engine, value.day, at, at+timedelta(seconds=60))
    assert storage.cooling_until(engine, value.day, at) == at+timedelta(seconds=60)
    assert storage.reserve_leg(engine, first, "own", "AAPL", at, 100, shared_cap=24000).reason == "cooldown"
    storage.halt(engine, value.day, "auth", at+timedelta(seconds=61))
    assert storage.halted(engine, value.day, at+timedelta(seconds=62)).reason == "auth"
    assert storage.reserve_leg(engine, first, "own", "AAPL", at+timedelta(seconds=62), 100, shared_cap=24000).reason == "day_halted"


def test_storage_readers_never_create_or_mutate_tables(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    recorded = own(engine, first)
    writes: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, parameters, context, executemany:
        writes.append(sql) if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) else None)
    storage.activation(engine, recorded.confirmed_at)
    storage.targets(engine, recorded.confirmed_at)
    storage.target_count(engine, recorded.confirmed_at)
    storage.attempts(engine, value.key)
    storage.legs(engine, first.key, recorded.confirmed_at)
    storage.completions(engine, value.key, recorded.confirmed_at)
    storage.assessments(engine, recorded.confirmed_at)
    storage.halted(engine, value.day, recorded.confirmed_at)
    assert writes == []


def test_bootstrap_reader_distinguishes_inflight_from_durable_completion(engine: Engine) -> None:
    value = target(engine)
    first = attempt(engine, value)
    active = storage.activation(engine, first.claimed_at)
    assert active is not None
    before = storage.read_open_attempts(engine, active, value.day, first.claimed_at)
    assert before.rows == ((first, value),) and not before.limited
    later = first.claimed_at+timedelta(seconds=1)
    storage.complete(engine, first, None, None, later, "quota_refused", clock=lambda: later)
    assert storage.read_open_attempts(engine, active, value.day, later).rows == ()


def test_malformed_packet_metadata_remains_unknown_without_report_exception(engine: Engine) -> None:
    recorded = own(engine, attempt(engine, target(engine)))
    with engine.begin() as connection:
        connection.execute(update(storage.TimedPacket).where(storage.TimedPacket.key == recorded.key).values(payload_json="{"))
    assert storage.evidence(engine, recorded.key, recorded.confirmed_at) is None


@pytest.mark.parametrize("kind", ["target", "assessment"])
def test_bound_target_and_terminal_numbers_cannot_be_corrupted_silently(engine: Engine, kind: str) -> None:
    value = target(engine)
    terminal = Assessment(key=value.key, target_key=value.key, assessed_at=value.deadline, status="missing", reason="no_attempt")
    storage.assess(engine, terminal)
    with engine.begin() as connection:
        if kind == "target":
            body = value.model_dump(mode="json")
            body["entry_reference"] = 1
            connection.execute(update(storage.TimedTarget).values(payload_json=json.dumps(body)))
        else:
            body = terminal.model_dump(mode="json")
            body["return_pct"] = 999
            connection.execute(update(storage.TimedAssessment).values(payload_json=json.dumps(body)))
    with pytest.raises(ValueError, match="integrity"):
        if kind == "target":
            storage.targets(engine, value.deadline)
        else:
            storage.assessments(engine, value.deadline)
