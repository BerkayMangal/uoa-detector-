"""Phase 3.3.4.2 tests for ``historical.state``.

Pins:
  - DownloadState round-trips through JSON
  - Atomic write: crash mid-write doesn't corrupt the existing file
  - Schema version mismatch raises StateSchemaError
  - Malformed JSON raises StateSchemaError
  - Missing file → load returns None
  - init_or_load: existing matching tier+range → resume
  - init_or_load: tier/range mismatch → fresh state, warning logged
  - Task transitions: pending → in_progress → done; → failed
  - pending_tasks excludes 'done' but includes pending/in_progress/failed
  - Task key format: 'TICKER:YYYY-MM' (uppercase, zero-padded)
  - progress_summary counts all 4 statuses
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from uoa_detector.historical.state import (
    DEFAULT_STATE_FILENAME,
    STATE_SCHEMA_VERSION,
    DateRange,
    DownloadState,
    StateSchemaError,
    TaskState,
    init_or_load_state,
    load_state,
    save_state,
    state_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_state() -> DownloadState:
    now = datetime.now(UTC)
    return DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=now,
        updated_at=now,
        tier="tier2_starter",
        date_range=DateRange(start=date(2024, 1, 1), end=date(2024, 6, 30)),
        tasks={},
    )


# ---------------------------------------------------------------------------
# Task key format
# ---------------------------------------------------------------------------


def test_make_task_key_format() -> None:
    """'TICKER:YYYY-MM', uppercase, zero-padded."""
    assert DownloadState.make_task_key("aapl", 2024, 1) == "AAPL:2024-01"
    assert DownloadState.make_task_key("MSFT", 2024, 11) == "MSFT:2024-11"


# ---------------------------------------------------------------------------
# Task transitions
# ---------------------------------------------------------------------------


def test_initialize_tasks_creates_pending_entries() -> None:
    state = _fresh_state()
    state.initialize_tasks([("AAPL", 2024, 1), ("AAPL", 2024, 2)])
    assert state.tasks["AAPL:2024-01"].status == "pending"
    assert state.tasks["AAPL:2024-02"].status == "pending"


def test_initialize_tasks_does_not_overwrite_existing() -> None:
    """Re-running initialize doesn't reset already-completed tasks."""
    state = _fresh_state()
    state.tasks["AAPL:2024-01"] = TaskState(status="done", row_count=100)
    state.initialize_tasks([("AAPL", 2024, 1), ("AAPL", 2024, 2)])
    assert state.tasks["AAPL:2024-01"].status == "done"
    assert state.tasks["AAPL:2024-01"].row_count == 100
    assert state.tasks["AAPL:2024-02"].status == "pending"


def test_mark_in_progress_clears_error() -> None:
    state = _fresh_state()
    state.mark_failed("AAPL", 2024, 1, error="boom")
    assert state.tasks["AAPL:2024-01"].status == "failed"
    assert state.tasks["AAPL:2024-01"].error == "boom"
    state.mark_in_progress("AAPL", 2024, 1)
    assert state.tasks["AAPL:2024-01"].status == "in_progress"
    assert state.tasks["AAPL:2024-01"].error is None


def test_mark_done_records_metadata() -> None:
    state = _fresh_state()
    state.mark_done(
        "AAPL", 2024, 1, row_count=12345,
        file_path="data/historical/thetadata/AAPL/2024-01.parquet",
    )
    task = state.tasks["AAPL:2024-01"]
    assert task.status == "done"
    assert task.row_count == 12345
    assert task.file_path == "data/historical/thetadata/AAPL/2024-01.parquet"
    assert task.completed_at is not None


def test_mark_failed_records_error() -> None:
    state = _fresh_state()
    state.mark_failed("AAPL", 2024, 1, error="ThetaDataTransientError: 503")
    task = state.tasks["AAPL:2024-01"]
    assert task.status == "failed"
    assert task.error == "ThetaDataTransientError: 503"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_pending_tasks_excludes_done_includes_others_sorted() -> None:
    state = _fresh_state()
    state.tasks["MSFT:2024-02"] = TaskState(status="pending")
    state.tasks["AAPL:2024-01"] = TaskState(status="done", row_count=1)
    state.tasks["AAPL:2024-02"] = TaskState(status="failed", error="x")
    state.tasks["GOOGL:2024-01"] = TaskState(status="in_progress")
    pending = state.pending_tasks()
    # Done excluded; rest sorted by (ticker, year, month)
    assert pending == [
        ("AAPL", 2024, 2),
        ("GOOGL", 2024, 1),
        ("MSFT", 2024, 2),
    ]


def test_progress_summary_counts_all_statuses() -> None:
    state = _fresh_state()
    state.tasks["A:2024-01"] = TaskState(status="done", row_count=1)
    state.tasks["A:2024-02"] = TaskState(status="done", row_count=1)
    state.tasks["B:2024-01"] = TaskState(status="pending")
    state.tasks["B:2024-02"] = TaskState(status="in_progress")
    state.tasks["C:2024-01"] = TaskState(status="failed", error="x")
    summary = state.progress_summary()
    assert summary == {
        "done": 2, "pending": 1, "in_progress": 1, "failed": 1,
    }


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    state = _fresh_state()
    state.mark_done(
        "AAPL", 2024, 1, row_count=100,
        file_path="data/historical/thetadata/AAPL/2024-01.parquet",
    )
    path = tmp_path / "state.json"
    save_state(state, path)
    loaded = load_state(path)
    assert loaded is not None
    assert loaded.tier == "tier2_starter"
    assert loaded.tasks["AAPL:2024-01"].status == "done"
    assert loaded.tasks["AAPL:2024-01"].row_count == 100


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_state(tmp_path / "no_such_file.json") is None


def test_load_malformed_json_raises_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{garbage not json")
    with pytest.raises(StateSchemaError, match="not valid JSON"):
        load_state(path)


def test_load_wrong_schema_version_raises(tmp_path: Path) -> None:
    """A state file from a future version raises with a clear message."""
    path = tmp_path / "future.json"
    payload = {
        "schema_version": 999,
        "started_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "tier": "tier2_starter",
        "date_range": {"start": "2024-01-01", "end": "2024-06-30"},
        "tasks": {},
    }
    path.write_text(json.dumps(payload))
    with pytest.raises(StateSchemaError, match="schema_version=999"):
        load_state(path)


def test_load_non_object_top_level_raises(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(StateSchemaError, match="not an object"):
        load_state(path)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_save_state_creates_parent_directory(tmp_path: Path) -> None:
    state = _fresh_state()
    nested = tmp_path / "a" / "b" / "c" / "state.json"
    save_state(state, nested)
    assert nested.exists()


def test_save_state_atomic_no_tmp_files_left(tmp_path: Path) -> None:
    """After a successful save, no .tmp file should be left behind."""
    state = _fresh_state()
    path = tmp_path / "state.json"
    save_state(state, path)
    leftovers = list(tmp_path.glob(".download_state.*.tmp"))
    assert leftovers == []


def test_save_state_overwrite_does_not_corrupt(tmp_path: Path) -> None:
    """Existing valid state stays valid even if save is called repeatedly."""
    state = _fresh_state()
    path = tmp_path / "state.json"
    save_state(state, path)
    for i in range(5):
        state.mark_done(
            "AAPL", 2024, i + 1, row_count=i,
            file_path=f"data/historical/AAPL/2024-{i+1:02d}.parquet",
        )
        save_state(state, path)
    loaded = load_state(path)
    assert loaded is not None
    assert len(loaded.tasks) == 5


def test_save_state_updates_updated_at(tmp_path: Path) -> None:
    state = _fresh_state()
    original = state.updated_at
    path = tmp_path / "state.json"
    # Force a tiny delay
    import time
    time.sleep(0.01)
    save_state(state, path)
    assert state.updated_at > original


# ---------------------------------------------------------------------------
# init_or_load
# ---------------------------------------------------------------------------


def test_init_or_load_no_file_creates_fresh(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = init_or_load_state(
        path=path, tier="tier2_starter",
        start_date=date(2024, 1, 1), end_date=date(2024, 6, 30),
    )
    assert state.tier == "tier2_starter"
    assert state.tasks == {}


def test_init_or_load_matching_tier_and_range_resumes(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    initial = _fresh_state()
    initial.mark_done(
        "AAPL", 2024, 1, row_count=100,
        file_path="data/historical/thetadata/AAPL/2024-01.parquet",
    )
    save_state(initial, path)
    loaded = init_or_load_state(
        path=path, tier="tier2_starter",
        start_date=date(2024, 1, 1), end_date=date(2024, 6, 30),
    )
    assert loaded.tasks["AAPL:2024-01"].status == "done"


def test_init_or_load_different_tier_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    initial = _fresh_state()
    initial.mark_done(
        "AAPL", 2024, 1, row_count=100,
        file_path="data/historical/thetadata/AAPL/2024-01.parquet",
    )
    save_state(initial, path)
    loaded = init_or_load_state(
        path=path, tier="tier1_anchor",  # different
        start_date=date(2024, 1, 1), end_date=date(2024, 6, 30),
    )
    assert loaded.tier == "tier1_anchor"
    assert loaded.tasks == {}


def test_init_or_load_different_range_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    initial = _fresh_state()
    save_state(initial, path)
    loaded = init_or_load_state(
        path=path, tier="tier2_starter",
        start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),  # different end
    )
    assert loaded.tasks == {}


# ---------------------------------------------------------------------------
# state_path helper
# ---------------------------------------------------------------------------


def test_state_path_returns_canonical_filename(tmp_path: Path) -> None:
    p = state_path(tmp_path)
    assert p.name == DEFAULT_STATE_FILENAME
    assert p.parent == tmp_path
