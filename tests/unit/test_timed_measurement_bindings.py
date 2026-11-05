"""Actual original publication provenance, activation, and fixed calendar targets."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from webapp.board.db import make_engine, session_factory
from webapp.live_alpha import store as live_store
from webapp.opening import storage as opening_storage
from webapp.timed_measurement import bindings, storage

from tests.unit.test_signal_explanation import T, publish, source


@pytest.fixture
def setup(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'bindings.db'}")
    live_store.ensure_live_tables(engine)
    opening_storage.ensure(engine)
    storage.ensure(engine)
    activation = storage.activate(engine, lambda: T - timedelta(seconds=1))
    yield engine, activation
    engine.dispose()


def original(*, spy: object = None):
    scan = source()
    scan.snapshot["price_observations"]["SPY"] = spy if spy is not None else {
        "spot": 500, "source_at": T.isoformat(), "session": T.date().isoformat(),
    }
    return scan


def test_actual_first_writer_binds_original_references_and_is_restart_idempotent(setup):
    engine, activation = setup
    call = publish(engine, original())
    with session_factory(engine)() as session:
        original_observations = session.scalar(select(func.count()).select_from(opening_storage.OpeningObservation))
    now = T + timedelta(minutes=1)
    targets = bindings.discover(engine, activation, now)
    assert len(targets) == 2
    assert {t.horizon: t.target_at for t in targets} == {
        "m15": call.at + timedelta(minutes=15), "m60": call.at + timedelta(minutes=60),
    }
    assert all(t.eligible and t.entry_reference == 100 and t.spy_entry == 500 for t in targets)
    assert all(t.decision_key == call.key and t.published_at == call.at for t in targets)
    assert bindings.discover(engine, activation, now + timedelta(seconds=10)) == ()
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(storage.TimedTarget)) == 2
        assert session.scalar(select(func.count()).select_from(opening_storage.OpeningObservation)) == original_observations
        assert session.scalar(select(func.count()).select_from(opening_storage.OpeningOutcome)) == 0


@pytest.mark.parametrize("damage", ["code", "primary", "snapshot", "scan_status", "cycle_scan", "profile", "first_decision"])
def test_unverified_first_source_never_becomes_a_requestable_target(setup, damage):
    engine, activation = setup
    call = publish(engine, original())
    with engine.begin() as connection:
        if damage == "code":
            raw = json.loads(connection.scalar(select(opening_storage.OpeningCycle.payload_json)))
            raw["code_hash"] = "f" * 64
            connection.execute(update(opening_storage.OpeningCycle).values(payload_json=json.dumps(raw)))
        elif damage == "primary":
            connection.execute(update(opening_storage.OpeningPrimary).where(opening_storage.OpeningPrimary.kind == "entry").values(key="wrong-key"))
        elif damage == "snapshot":
            connection.execute(update(live_store.LiveScan).values(snapshot_json="{}"))
        elif damage == "scan_status":
            connection.execute(update(live_store.LiveScan).values(status="failed"))
        elif damage == "cycle_scan":
            connection.execute(update(opening_storage.OpeningCycle).values(scan_id="another-scan"))
        elif damage == "profile":
            activation = activation.model_copy(update={"opening_profile_hash": "f" * 64})
        else:
            connection.execute(update(opening_storage.OpeningPrimary).values(decision_key="another-decision"))
    assert bindings.discover(engine, activation, call.at + timedelta(seconds=1)) == ()


def test_pre_activation_first_call_cannot_be_replaced_by_post_activation_update(setup):
    engine, _ = setup
    first = publish(engine, original())
    # A separate boundary is supplied purely as a fixture; no production records
    # or retrospective activations are written.
    activation = storage.activation(engine, first.at).model_copy(update={"activated_at": first.at + timedelta(seconds=1)})
    publish(engine, source(at=T + timedelta(minutes=1), identity="update"))
    assert bindings.discover(engine, activation, T + timedelta(minutes=2)) == ()


@pytest.mark.parametrize("spy", [
    {}, {"spot": 0, "source_at": T.isoformat(), "session": T.date().isoformat()},
    {"spot": 500, "source_at": (T + timedelta(seconds=4)).isoformat(), "session": T.date().isoformat()},
    {"spot": 500, "source_at": (T - timedelta(minutes=6)).isoformat(), "session": T.date().isoformat()},
    {"spot": 500, "source_at": T.isoformat(), "session": "2026-11-03"},
    {"spot": 500, "source_at": T.replace(tzinfo=None).isoformat(), "session": T.date().isoformat()},
    {"spot": 500, "source_at": T.isoformat(), "session": T.date().isoformat(), "ticker": "QQQ"},
])
def test_missing_original_spy_stays_unknown_without_disabling_own_reference(setup, spy):
    engine, activation = setup
    publish(engine, original(spy=spy))
    targets = bindings.discover(engine, activation, T + timedelta(minutes=1))
    assert len(targets) == 2 and all(t.eligible and t.spy_entry is None for t in targets)


def test_late_discovery_is_recorded_without_retrospective_price_lookup(setup):
    engine, activation = setup
    publish(engine, original())
    targets = bindings.discover(engine, activation, T + timedelta(hours=2))
    assert len(targets) == 2 and all(t.reason == "late_discovery" for t in targets)
    assert all(t.deadline < t.discovered_at for t in targets)


def test_changed_advice_entry_cannot_replace_original_underlying_plan(setup):
    engine, activation = setup
    call = publish(engine, original())
    with engine.begin() as connection:
        raw = json.loads(connection.scalar(select(opening_storage.OpeningDecision.payload_json)
                         .where(opening_storage.OpeningDecision.key == call.key)))
        raw["plan"]["entry"] = 200
        connection.execute(update(opening_storage.OpeningDecision)
                           .where(opening_storage.OpeningDecision.key == call.key)
                           .values(payload_json=json.dumps(raw)))
    targets = bindings.discover(engine, activation, T + timedelta(minutes=1))
    assert len(targets) == 2
    assert all(not t.eligible and t.entry_reference is None and t.reason == "missing_entry_reference" for t in targets)


def test_spy_timestamp_after_finished_snapshot_is_not_known_entry_data(setup):
    engine, activation = setup
    publish(engine, original(spy={"spot": 500, "source_at": (T + timedelta(seconds=2, microseconds=1)).isoformat(),
                                 "session": T.date().isoformat()}))
    targets = bindings.discover(engine, activation, T + timedelta(minutes=1))
    assert len(targets) == 2 and all(t.eligible and t.spy_entry is None for t in targets)


@pytest.mark.parametrize(("at", "minutes", "target_hour", "eligible"), [
    (datetime(2026, 10, 30, 13, 45, tzinfo=UTC), 15, 14, True),
    (datetime(2026, 11, 2, 14, 45, tzinfo=UTC), 15, 15, True),
    (datetime(2026, 11, 27, 17, 45, tzinfo=UTC), 15, 18, False),
    (datetime(2026, 11, 26, 14, 45, tzinfo=UTC), 15, 15, False),
    (datetime(2035, 11, 5, 14, 45, tzinfo=UTC), 15, 15, False),
])
def test_fixed_targets_respect_dst_half_day_holiday_and_unknown_calendar(at, minutes, target_hour, eligible):
    target, _deadline, actual, _reason = bindings.window(at, minutes)
    assert target.hour == target_hour and actual is eligible


def test_target_capture_window_is_clipped_at_half_day_close():
    target, deadline, applicable, _ = bindings.window(datetime(2026, 11, 27, 17, 43, tzinfo=UTC), 15)
    assert applicable and target.hour == 17 and target.minute == 58
    assert deadline == datetime(2026, 11, 27, 18, tzinfo=UTC)
