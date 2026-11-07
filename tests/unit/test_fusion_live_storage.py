"""Durable forward decisions cannot acquire repaired or backdated availability."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.fusion_live import config, storage
from webapp.shadow_capture.model import digest

NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)
IDENTITY = {"version": "fusion_live_v1", "profile_hash": "a" * 64, "implementation_hash": "b" * 64}


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "identity", lambda: dict(IDENTITY))
    engine = make_engine(f"sqlite:///{tmp_path / 'fusion-storage.db'}")
    storage.ensure(engine)
    yield engine
    engine.dispose()


def put(engine: Engine, key: str = "decision-one", **kwargs: Any) -> storage.Record | None:
    values: dict[str, Any] = dict(
        key=key,
        kind="decision",
        at=NOW,
        day=NOW.date(),
        candidate_key="source-candidate",
        arm="semantic_positive",
        payload={"identity": IDENTITY, "state": "PAPER_ACCEPT"},
        clock=lambda: NOW + timedelta(seconds=1),
    )
    values.update(kwargs)
    return storage.put(engine, **values)


def test_availability_is_stamped_after_event_and_first_receipt_commits(engine: Engine) -> None:
    clock_reads = []

    def clock() -> datetime:
        with Session(engine) as session:
            assert session.get(storage.FusionEvent, "decision-one") is not None
            count = session.scalar(select(func.count()).select_from(storage.FusionReceipt))
        clock_reads.append(count)
        return NOW + timedelta(seconds=len(clock_reads))

    row = put(engine, clock=clock)
    assert row is not None and row.at == NOW and row.available_at == NOW + timedelta(seconds=2)
    assert clock_reads == [0, 1]
    assert storage.get(engine, row.key, NOW + timedelta(seconds=1)) is None
    pending = storage.get(engine, row.key, NOW + timedelta(seconds=1), confirmed=False)
    assert pending is not None and pending.available_at is None


def test_delayed_first_receipt_commit_moves_physical_confirmation_forward(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_at = NOW + timedelta(seconds=1)
    original = storage._insert

    def insert(e: Engine, model: Any, values: dict[str, Any]) -> bool:
        nonlocal clock_at
        inserted = original(e, model, values)
        if model is storage.FusionReceipt and values["stage"] == "recorded":
            clock_at += timedelta(seconds=90)
        return inserted

    monkeypatch.setattr(storage, "_insert", insert)
    row = put(engine, clock=lambda: clock_at)
    assert row is not None and row.available_at == NOW + timedelta(seconds=91)
    assert storage.get(engine, row.key, NOW + timedelta(seconds=89)) is None


def test_duplicate_decision_does_not_replace_payload_or_reconfirm(engine: Engine) -> None:
    row = put(engine)
    assert row is not None
    assert (
        put(engine, payload={"state": "PAPER_SKIP"}, clock=lambda: NOW + timedelta(hours=1)) is None
    )
    assert storage.get(engine, row.key, NOW + timedelta(hours=1)) == row
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(storage.FusionReceipt)) == 2


def test_concurrent_claimants_obtain_only_one_immutable_decision(engine: Engine) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = tuple(pool.map(lambda _: put(engine), range(4)))
    assert sum(row is not None for row in records) == 1
    row = storage.get(engine, "decision-one", NOW + timedelta(seconds=2))
    assert row is not None and row.payload["state"] == "PAPER_ACCEPT"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(storage.FusionEvent)) == 1
        assert session.scalar(select(func.count()).select_from(storage.FusionReceipt)) == 2


@pytest.mark.parametrize("stage", ["recorded", "confirmed"])
def test_unconfirmed_old_decision_never_receives_repaired_receipts(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    original = storage._insert

    def crash(e: Engine, model: Any, values: dict[str, Any]) -> bool:
        if model is storage.FusionReceipt and values["stage"] == stage:
            raise RuntimeError("synthetic receipt write failure")
        return original(e, model, values)

    monkeypatch.setattr(storage, "_insert", crash)
    with pytest.raises(RuntimeError, match="synthetic"):
        put(engine)
    monkeypatch.setattr(storage, "_insert", original)
    late = NOW + timedelta(minutes=5)
    assert put(engine, clock=lambda: late) is None
    assert storage.get(engine, "decision-one", late) is None
    raw = storage.get(engine, "decision-one", late, confirmed=False)
    assert raw is not None and raw.available_at is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(storage.FusionReceipt)) == (
            0 if stage == "recorded" else 1
        )


@pytest.mark.parametrize("stage", ["recorded", "confirmed"])
def test_only_activation_recovers_missing_receipt_at_actual_restart_time(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    original = storage._insert

    def crash(e: Engine, model: Any, values: dict[str, Any]) -> bool:
        if model is storage.FusionReceipt and values["stage"] == stage:
            raise RuntimeError("synthetic activation interruption")
        return original(e, model, values)

    monkeypatch.setattr(storage, "_insert", crash)
    with pytest.raises(RuntimeError, match="synthetic"):
        storage.activate(engine, lambda: NOW)
    monkeypatch.setattr(storage, "_insert", original)
    resumed_at = NOW + timedelta(minutes=5)
    active = storage.activate(engine, lambda: resumed_at)
    assert active.at == NOW and active.available_at == resumed_at
    assert storage.active(engine, NOW + timedelta(minutes=4)) is None
    assert storage.active(engine, resumed_at) == active


@pytest.mark.parametrize("field", ["key", "parent_key", "stage", "checksum", "at"])
def test_wrong_or_replayed_receipt_binding_is_not_confirmed(engine: Engine, field: str) -> None:
    row = put(engine)
    assert row is not None
    value: object = NOW - timedelta(seconds=1) if field == "at" else "wrong-binding"
    with engine.begin() as connection:
        connection.execute(
            update(storage.FusionReceipt)
            .where(storage.FusionReceipt.stage == "recorded")
            .values(**{field: value})
        )
    assert storage.get(engine, row.key, NOW + timedelta(seconds=5)) is None
    assert (
        storage.get(engine, row.key, NOW + timedelta(seconds=5), confirmed=False).available_at
        is None
    )


@pytest.mark.parametrize("field", ["candidate_key", "arm", "kind", "payload_json", "checksum"])
def test_parent_and_policy_metadata_tampering_never_becomes_verified(
    engine: Engine, field: str
) -> None:
    row = put(engine)
    assert row is not None
    with engine.begin() as connection:
        connection.execute(
            update(storage.FusionEvent)
            .where(storage.FusionEvent.key == row.key)
            .values(**{field: "tampered"})
        )
    assert storage.get(engine, row.key, NOW + timedelta(seconds=5)) is None


def test_even_coherently_checksummed_receipts_cannot_precede_the_parent(engine: Engine) -> None:
    row = put(engine)
    assert row is not None
    first_at, final_at = NOW - timedelta(seconds=2), NOW - timedelta(seconds=1)
    first_sum = digest((row.key, row.checksum, "recorded", first_at.isoformat()))
    final_sum = digest(
        (row.key, row.checksum, "confirmed", first_sum, first_at.isoformat(), final_at.isoformat())
    )
    with engine.begin() as connection:
        connection.execute(
            update(storage.FusionReceipt)
            .where(storage.FusionReceipt.stage == "recorded")
            .values(at=first_at, checksum=first_sum)
        )
        connection.execute(
            update(storage.FusionReceipt)
            .where(storage.FusionReceipt.stage == "confirmed")
            .values(at=final_at, checksum=final_sum)
        )
    assert storage.get(engine, row.key, NOW + timedelta(seconds=5)) is None


def test_future_events_and_receipts_are_unavailable_but_continue_to_own_the_key(
    engine: Engine,
) -> None:
    later = NOW + timedelta(minutes=1)
    row = put(engine, at=later, clock=lambda: later)
    assert row is not None
    assert storage.get(engine, row.key, NOW, confirmed=False) is None
    assert storage.list_events(engine, "decision", NOW, confirmed=False) == ()
    assert put(engine) is None
    assert storage.get(engine, row.key, later) == row


def test_reversed_commit_clock_leaves_an_unconfirmed_record_that_cannot_replay(
    engine: Engine,
) -> None:
    with pytest.raises(ValueError, match="fusion_clock_reversed"):
        put(engine, clock=lambda: NOW - timedelta(seconds=1))
    assert put(engine, clock=lambda: NOW + timedelta(hours=1)) is None
    assert storage.get(engine, "decision-one", NOW + timedelta(hours=1)) is None


def test_reads_do_not_create_records_ddl_or_confirm_pending_work(engine: Engine) -> None:
    row = put(engine)
    assert row is not None
    statements: list[str] = []

    def before(_c: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", before)
    assert storage.available(engine)
    assert storage.get(engine, row.key, NOW + timedelta(seconds=2)) == row
    assert storage.list_events(engine, "decision", NOW + timedelta(seconds=2), day=NOW.date()) == (
        row,
    )
    assert storage.active(engine, NOW + timedelta(seconds=2)) is None
    assert not any(
        sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
        for sql in statements
    )


def test_storage_query_rejects_unbounded_read_limits(engine: Engine) -> None:
    for limit in (0, -1, 513):
        with pytest.raises(ValueError, match="fusion_read_limit"):
            storage.list_events(engine, "decision", NOW, limit=limit)
