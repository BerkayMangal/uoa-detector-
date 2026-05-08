"""``BacktestStoreProtocol`` — unified contract for backtest stores.

Phase 3.2.1: introduces this Protocol so the orchestrator (and every
test) can talk to either the in-memory ``BacktestStore`` or the
``SqliteBacktestStore`` without caring which is in use. Both
implementations satisfy this Protocol.

The Protocol is structural (``runtime_checkable``) so tests can assert
``isinstance(store, BacktestStoreProtocol)`` for either backend without
explicit registration.

Lifecycle (per the post-implementation clarification in
docs/phase-3.2-acceptance.md):

  * **Permissive default** (``strict_run_lifecycle=False``): the first
    ``add()`` call without a prior ``start_run()`` auto-creates an
    "implicit" run and subsequent ``add()`` calls write to it. Phase
    1-2 call sites need no changes.

  * **Strict opt-in** (``strict_run_lifecycle=True``): ``add()`` and
    ``record_error()`` raise ``RunLifecycleError`` if no run is
    active. Phase 3.2.4's 4-cell runner uses this to guarantee each
    cell has its own run_id with no implicit-run leakage.

``schema_version`` returns ``int | None``: the SQLite implementation
returns the current Alembic-tracked version (≥ 1); the in-memory
implementation returns ``None`` because there is no persistent state
to migrate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from uoa_detector.backtest.models import ErrorRecord, RunMetadata
    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration import CalibrationProfile
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.domain.labels import LabelDecision
    from uoa_detector.domain.risk import PositionSize


@runtime_checkable
class BacktestStoreProtocol(Protocol):
    """The minimal surface any backtest store must implement."""

    @property
    def schema_version(self) -> int | None:
        """Persistent-schema version (SQLite) or ``None`` (in-memory)."""

    @property
    def active_run_id(self) -> str | None:
        """The ``run_id`` of the currently-active run, or ``None``.

        Used by callers (e.g., ``Pipeline.run()``) to decide whether to
        call ``finish_run()`` on a store they don't own — without this
        property they would have to track lifecycle externally or call
        ``finish_run()`` blindly (which is fine in permissive mode but
        raises in strict mode when no run is active).
        """

    def start_run(
        self,
        profile: CalibrationProfile,
        *,
        universe_id: str | None = None,
        source_config_hash: str | None = None,
        dataset_window_start: datetime | None = None,
        dataset_window_end: datetime | None = None,
        notes: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Open a new run; returns its ``run_id``.

        If a run is already active and not yet finished, the
        implementation should either auto-finish it (permissive) or
        raise ``RunLifecycleError`` (strict). Both backends auto-finish.

        ``run_id`` is generated (uuid4 hex) when not provided.
        """

    def add(
        self,
        event: EnrichedEvent,
        decision: LabelDecision,
        size: PositionSize,
        *,
        pipeline_latency_ms: float | None = None,
        data_source_latency_ms: float | None = None,
    ) -> StoredSignal:
        """Persist one signal to the active run.

        Permissive mode auto-creates an implicit run on first call;
        strict mode raises ``RunLifecycleError`` instead.
        """

    def record_error(
        self,
        *,
        stage_name: str,
        error_type: str,
        error_message: str,
        event_id: str | None = None,
    ) -> ErrorRecord:
        """Append a stage-error record to the active run.

        Increments ``RunMetadata.total_errors`` for that run.
        """

    def finish_run(self) -> RunMetadata | None:
        """Close the active run; returns its final metadata.

        ``None`` if no run is active and the store is permissive
        (idempotent close). Strict mode raises ``RunLifecycleError``.
        """

    def get_run(self, run_id: str) -> RunMetadata | None:
        """Look up a run by id; ``None`` if not found."""

    def list_runs(self) -> list[RunMetadata]:
        """All known runs, ordered by ``started_at`` descending."""

    def iter_records(self, run_id: str) -> Iterator[StoredSignal]:
        """Stream the signals belonging to ``run_id``.

        Streaming so a multi-million-row run does not have to fit in
        RAM. Order: insertion order (== event-time order, since events
        are added as they're processed).
        """

    def iter_errors(self, run_id: str) -> Iterator[ErrorRecord]:
        """Stream the errors belonging to ``run_id``."""

    def update_signal_score(
        self,
        run_id: str,
        event_id: str,
        score_name: str,
        value: float | None,
    ) -> bool:
        """Update a single sub-score on a stored signal.

        Phase 3.4.8 BREAKING CHANGE: required for post-event
        validators (Module 28's next-day OI confirmation runs as
        a nightly batch and writes back the validation score to
        signals from prior session). Allowed score_name values
        are the float-typed sub-score fields on StoredSignal:

          - opening_closing_score (M27)
          - m28_confirmation_score (M28)
          - any future float sub-score field

        Returns True if a row matched and was updated; False if
        no signal with (run_id, event_id) was found.

        Implementations should be idempotent: re-applying the
        same (run_id, event_id, score_name, value) is a no-op
        beyond a single UPDATE statement.

        Setting ``value=None`` clears the score (useful for
        invalidation/retry workflows).
        """

    def close(self) -> None:
        """Flush any buffered writes and release resources.

        In-memory implementation: no-op.
        SQLite implementation: flush pending rows, finish the active run
        if any, dispose the engine.
        """
