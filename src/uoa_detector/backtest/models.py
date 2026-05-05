"""Shared domain models for the backtest store layer.

Phase 3.2.1: introduces ``RunMetadata`` (one row per backtest invocation)
and ``ErrorRecord`` (one row per stage error). Both store implementations
(in-memory and SQLite) round-trip these models identically; the metric
calculator (Phase 3.2.3) reads them via the ``BacktestStoreProtocol``
without caring which backend is in use.

Design notes:

  * ``run_id`` is a string (uuid4 hex by default) so callers can
    generate them externally if needed (e.g., the 4-cell runner in
    3.2.4 can assign deterministic run_ids per cell for reproducibility).

  * ``profile_content_hash`` lives on every ``RunMetadata`` AND every
    individual ``StoredSignal``. Redundant, intentionally — Phase 3.3.x
    cross-cell comparison wants to assert "same hash on every signal,
    same hash on the run row" without doing a JOIN.

  * Timestamps are timezone-aware (UTC). The audit trail must survive
    log rotation, file moves, and zone migrations, so explicit UTC is
    required not optional.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RunMetadata(BaseModel):
    """One row of the ``backtest_run`` table.

    All cross-cell-comparison metadata lives here: which profile,
    which universe, which sources, which dataset window, plus the
    bookkeeping (signals processed, errors encountered) that lets
    Phase 3.3.x assert a clean run before reading metric deltas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime | None
    profile_id: str
    profile_content_hash: str
    universe_id: str | None
    source_config_hash: str | None
    total_signals_processed: int
    total_errors: int
    dataset_window_start: datetime | None
    dataset_window_end: datetime | None
    notes: str | None


class ErrorRecord(BaseModel):
    """One row of the ``backtest_run_error`` table.

    Stage errors used to be log-and-lost in Phase 1-2. From 3.2.1 onward
    they're persistent so a backtest report can show "this run threw 47
    errors in stage M37 between 2025-04-12 and 2025-04-15" — which
    Phase 3.3.x's "ran cleanly" precondition needs to reject silently
    successful but actually-broken runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    event_id: str | None
    stage_name: str
    error_type: str
    error_message: str
    occurred_at: datetime
