"""A maintenance lock must leave room for the legacy verifier's nested inspection."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import QueuePool
from webapp.calibration.storage import DailyBar
from webapp.call_research import coverage, repair_operator
from webapp.call_research.storage import DailyReceipt
from webapp.daily_guardian.model import coverage_snapshot

from tests.unit.test_daily_guardian_service import NAMES, NOW, seed


def prepared_engine(tmp_path, monkeypatch, size):
    cfg = coverage.settings().model_copy(update={"persistence_sessions": 3, "query_limit": 1000})
    monkeypatch.setattr(coverage, "settings", lambda: cfg)
    monkeypatch.setattr(coverage, "targets", lambda: NAMES)
    engine = create_engine(
        f"sqlite:///{tmp_path / ('pool-' + str(size) + '.db')}",
        poolclass=QueuePool,
        pool_size=size,
        max_overflow=0,
        pool_timeout=0.05,
    )
    for model in (DailyBar, DailyReceipt, repair_operator.RepairRecord):
        model.__table__.create(engine)
    seed(engine)
    return engine


def test_two_slots_exhaust_when_maintenance_lock_holds_one(tmp_path, monkeypatch):
    engine = prepared_engine(tmp_path, monkeypatch, 2)
    try:
        # Production admission holds one physical session.
        with engine.connect(), pytest.raises(PoolTimeoutError):
            coverage_snapshot(engine, NOW)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_three_slots_allow_real_receipt_verifier_nested_inspection(tmp_path, monkeypatch):
    engine = prepared_engine(tmp_path, monkeypatch, 3)
    counts = {"active": 0, "peak": 0}

    def checked_out(_connection, _record, _proxy):
        counts["active"] += 1
        counts["peak"] = max(counts["peak"], counts["active"])

    def checked_in(_connection, _record):
        counts["active"] -= 1

    event.listen(engine, "checkout", checked_out)
    event.listen(engine, "checkin", checked_in)
    try:
        with engine.connect():
            snapshot = coverage_snapshot(engine, NOW)
            assert snapshot.status == "ready"
            assert snapshot.ready == snapshot.expected == len(NAMES)
            assert snapshot.unverified == 0
        assert counts == {"active": 0, "peak": 3}
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
