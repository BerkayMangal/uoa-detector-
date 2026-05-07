"""Download state — ``.download_state.json`` atomic I/O + resume.

Phase 3.3.4.2: tracks per-(ticker, year, month) download status so
a crashed or interrupted bulk run can resume from where it left
off without re-fetching completed months.

State file layout::

  data/historical/.download_state.json

  {
    "schema_version": 1,
    "started_at": "2024-11-15T18:00:00+00:00",
    "updated_at": "2024-11-15T19:42:13+00:00",
    "tier": "tier2_starter",
    "date_range": {"start": "2024-01-01", "end": "2024-06-30"},
    "tasks": {
      "AAPL:2024-01": {
        "status": "done",
        "row_count": 124533,
        "file_path": "data/historical/thetadata/AAPL/2024-01.parquet",
        "completed_at": "2024-11-15T18:42:11+00:00",
        "error": null
      },
      "AAPL:2024-02": {
        "status": "failed",
        "row_count": 0,
        "file_path": null,
        "completed_at": null,
        "error": "ThetaDataTransientError: HTTP 503 after 3 attempts"
      },
      "MSFT:2024-01": {"status": "pending", ...},
      ...
    }
  }

decision (atomic write via tmp + os.replace):
  Crash mid-write must not corrupt the state file. We write to
  ``{path}.tmp`` and ``os.replace()`` it onto the real path.
  os.replace is atomic on POSIX and Windows for files on the
  same filesystem. This is the same pattern Phase 3.3.2.4's
  parquet writer uses.

decision (one task per (ticker, year, month), not per-contract):
  The downloader writes one parquet per (ticker, month). State
  resumes at this granularity. A failed contract within a month
  re-runs the whole month — wasteful but simple. Phase 3.4 may
  refine to per-contract resume if needed.

decision (status enum: pending / in_progress / done / failed):
  - pending: not yet attempted (initial state)
  - in_progress: a worker is currently fetching (set on dispatch,
    cleared on completion or failure)
  - done: parquet written + verified row_count > 0
  - failed: an error occurred; .error field has the message
  Resume policy: pending + in_progress + failed are all retried.
  in_progress means a previous run crashed mid-fetch; safe to retry
  because the downloader is idempotent.

decision (key format 'TICKER:YYYY-MM' — colon-delimited):
  Human-readable when grepping the state file. Stable sort order
  by (ticker, year, month). Uppercase ticker matches the
  normalisation in universe.py.

decision (no JSON Schema validation library — Pydantic):
  Pydantic is already in scope; adding jsonschema would duplicate.
  DownloadState model uses Pydantic with ``extra='forbid'`` so
  unknown fields in older state files raise a clear error.

decision (schema_version = 1, raise on mismatch):
  Forward-compat: when we change the state format (e.g., per-
  contract granularity in Phase 3.4), bump schema_version. Loading
  an older version raises StateSchemaError; the operator must
  delete and restart.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_FILENAME = ".download_state.json"

TaskStatus = Literal["pending", "in_progress", "done", "failed"]


class StateSchemaError(RuntimeError):
    """Raised when a loaded state file's schema_version doesn't match."""


class DateRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    start: date
    end: date


class TaskState(BaseModel):
    """One task's progress."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    status: TaskStatus = "pending"
    row_count: int = 0
    file_path: str | None = None
    completed_at: datetime | None = None
    error: str | None = None


class DownloadState(BaseModel):
    """Full state file payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = STATE_SCHEMA_VERSION
    started_at: datetime
    updated_at: datetime
    tier: str
    date_range: DateRange
    tasks: dict[str, TaskState] = Field(default_factory=dict)

    # -- task helpers ----------------------------------------------------

    @staticmethod
    def make_task_key(ticker: str, year: int, month: int) -> str:
        return f"{ticker.upper()}:{year:04d}-{month:02d}"

    def initialize_tasks(
        self,
        ticker_year_month_tuples: list[tuple[str, int, int]],
    ) -> None:
        """Add 'pending' entries for every task not already present."""
        for ticker, year, month in ticker_year_month_tuples:
            key = self.make_task_key(ticker, year, month)
            if key not in self.tasks:
                self.tasks[key] = TaskState(status="pending")

    def mark_in_progress(self, ticker: str, year: int, month: int) -> None:
        key = self.make_task_key(ticker, year, month)
        task = self.tasks.get(key) or TaskState()
        task.status = "in_progress"
        task.error = None
        self.tasks[key] = task

    def mark_done(
        self,
        ticker: str, year: int, month: int,
        *,
        row_count: int,
        file_path: str,
    ) -> None:
        key = self.make_task_key(ticker, year, month)
        task = self.tasks.get(key) or TaskState()
        task.status = "done"
        task.row_count = row_count
        task.file_path = file_path
        task.completed_at = datetime.now(UTC)
        task.error = None
        self.tasks[key] = task

    def mark_failed(
        self,
        ticker: str, year: int, month: int,
        *,
        error: str,
    ) -> None:
        key = self.make_task_key(ticker, year, month)
        task = self.tasks.get(key) or TaskState()
        task.status = "failed"
        task.error = error
        task.completed_at = datetime.now(UTC)
        self.tasks[key] = task

    # -- queries --------------------------------------------------------

    def pending_tasks(self) -> list[tuple[str, int, int]]:
        """Tasks needing work — pending + in_progress + failed.

        Sorted by (ticker, year, month) for deterministic dispatch.
        """
        out: list[tuple[str, int, int]] = []
        for key, task in self.tasks.items():
            if task.status == "done":
                continue
            ticker, ym = key.split(":", 1)
            year_str, month_str = ym.split("-", 1)
            out.append((ticker, int(year_str), int(month_str)))
        out.sort()
        return out

    def progress_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "pending": 0, "in_progress": 0, "done": 0, "failed": 0,
        }
        for task in self.tasks.values():
            counts[task.status] += 1
        return counts


# ---------------------------------------------------------------------------
# I/O — atomic
# ---------------------------------------------------------------------------


def state_path(historical_dir: Path) -> Path:
    """Canonical state file location: ``{historical_dir}/.download_state.json``."""
    return historical_dir / DEFAULT_STATE_FILENAME


def load_state(path: Path) -> DownloadState | None:
    """Load state from disk, or None if the file doesn't exist.

    Raises StateSchemaError if the file exists but its schema_version
    doesn't match the current STATE_SCHEMA_VERSION.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"state file {path} is not valid JSON: {exc}"
        raise StateSchemaError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"state file {path} top-level is not an object"
        raise StateSchemaError(msg)
    version = parsed.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        msg = (
            f"state file {path} has schema_version={version}, "
            f"expected {STATE_SCHEMA_VERSION} — delete and restart"
        )
        raise StateSchemaError(msg)
    return DownloadState.model_validate(parsed)


def save_state(state: DownloadState, path: Path) -> None:
    """Atomic write: tmp + os.replace.

    Updates state.updated_at to now() before serialising. Creates
    the parent directory if it doesn't exist.
    """
    state.updated_at = datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    text = json.dumps(payload, indent=2, sort_keys=True)
    # Use NamedTemporaryFile in the same directory for same-fs atomicity
    fd, tmp_str = tempfile.mkstemp(
        dir=path.parent, prefix=".download_state.", suffix=".tmp",
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def init_or_load_state(
    *,
    path: Path,
    tier: str,
    start_date: date,
    end_date: date,
) -> DownloadState:
    """Load existing state if compatible; else create a fresh one.

    'Compatible' = same tier + same date_range. Different tier or
    range → fresh state (the operator is starting a new run).
    """
    existing = load_state(path)
    if existing is not None:
        same_tier = existing.tier == tier
        same_range = (
            existing.date_range.start == start_date
            and existing.date_range.end == end_date
        )
        if same_tier and same_range:
            _logger.info(
                "Resuming existing state at %s — %d tasks already tracked",
                path, len(existing.tasks),
            )
            return existing
        _logger.warning(
            "State at %s differs (tier=%s vs %s, range=%s..%s vs %s..%s) "
            "— starting fresh",
            path, existing.tier, tier,
            existing.date_range.start, existing.date_range.end,
            start_date, end_date,
        )
    now = datetime.now(UTC)
    return DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=now,
        updated_at=now,
        tier=tier,
        date_range=DateRange(start=start_date, end=end_date),
        tasks={},
    )
