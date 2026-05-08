"""In-memory backtest store. Phase 3.2.1: extended to implement
``BacktestStoreProtocol`` (run lifecycle, latency tracking, error
recording) so it stands in as a drop-in alternative to the SQLite
store. Phase 1-2 surface is preserved.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.backtest.models import ErrorRecord, RunMetadata
from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize
from uoa_detector.errors import RunLifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from uoa_detector.calibration import CalibrationProfile

OptionType = Literal["call", "put"]


class StoredSignal(BaseModel):
    """One row of the backtest store. Field groups follow Module 29."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    timestamp: datetime
    ticker: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    dte: int

    # Pricing
    premium: Decimal
    option_price: Decimal
    moneyness: Decimal  # spot / strike

    # Sub-scores (v5)
    uoa_score: float | None
    convexity_score: float | None
    event_score: float | None
    gamma_score: float | None
    price_confirmation_score: float | None
    sector_confirmation_score: float | None
    time_of_day_weight: float | None
    cluster_density_score: float | None
    relative_premium_score: float | None
    dte_multiplier_applied: float | None

    # Combined scores
    combined_score_pre_penalty: float | None
    combined_score_post_penalty: float | None
    penalties_applied: list[AppliedPenalty] = Field(default_factory=list)
    contradiction_penalty_applied: bool = False

    # Classification
    sweep_classification: str | None
    is_iso: bool
    gamma_flag: bool  # GAMMA_ACCELERATION_RISK on this event
    sector_confirmation: bool
    dark_pool_confirmation: bool

    # Decision
    label: SignalLabel
    label_reason: str
    max_r: float
    scale_in: bool
    initial_r: float | None

    # Outcomes — populated by a later phase
    spot_return_1h: float | None = None
    spot_return_1d: float | None = None
    spot_return_3d: float | None = None
    spot_return_5d: float | None = None
    spot_return_10d: float | None = None
    iv_change_after_signal: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    final_label: SignalLabel | None = None
    next_day_oi_confirmed: bool | None = None

    # Phase 3.4.8: Module 27 (Opening/closing OI delta) score —
    # populated at signal creation by M27 stage. Phase 3.4.7
    # treated this as internal/telemetry; 3.4.8 promotes to a
    # persisted field because Module 28 needs to filter signals
    # to validate.
    opening_closing_score: float | None = None
    # Phase 3.4.8: Module 28 (Next-day OI confirmation) score.
    # POPULATED POST-EVENT — written by run_m28_overnight at T+1
    # via store.update_signal_score(). None means "not yet
    # validated"; non-None means M28 has run.
    m28_confirmation_score: float | None = None

    # Phase 3.2.1: run association + latency tracking. Optional so
    # Phase 1-2 call sites that don't measure latency keep working.
    # ``run_id`` is populated by the store at insert time (auto-implicit
    # run in permissive mode, explicit run in strict mode); ``event_id``
    # mirrors EnrichedEvent.print_.event_id for cross-table lookups.
    run_id: str | None = None
    event_id: str | None = None
    pipeline_latency_ms: float | None = None
    data_source_latency_ms: float | None = None


class BacktestStore:
    """In-memory backtest store implementing ``BacktestStoreProtocol``.

    Phase 3.2.1: extended from a flat list to a per-run aggregate so it
    can stand in as a drop-in alternative to the SQLite store for tests
    and lightweight runs. Phase 1-2 call sites continue to work
    unchanged because:

      - The constructor accepts no required arguments (still ``BacktestStore()``).
      - ``add(event, decision, size)`` keeps its 3-arg signature; the
        new ``pipeline_latency_ms`` and ``data_source_latency_ms`` are
        keyword-only and default to ``None``.
      - ``all()``, ``__len__``, and ``to_dataframe()`` keep their Phase
        1-2 semantics: they enumerate every signal across every run,
        ordered by insertion. Single-run uses see the same surface
        they always saw.

    Run lifecycle (per the post-implementation clarification in
    docs/phase-3.2-acceptance.md):

      - Default ``strict_run_lifecycle=False``: the first ``add()``
        call without a prior ``start_run()`` auto-creates an "implicit"
        run with ``run_id="implicit-default"``; subsequent ``add()``
        calls write to it; closing the store auto-finishes.
      - ``strict_run_lifecycle=True``: ``add()`` and ``record_error()``
        raise ``RunLifecycleError`` if no run is active. Required for
        Phase 3.2.4's 4-cell runner where each cell must have a unique
        ``run_id`` and implicit-run leakage would corrupt the
        comparison.
    """

    _IMPLICIT_RUN_ID: ClassVar[str] = "implicit-default"

    def __init__(self, *, strict_run_lifecycle: bool = False) -> None:
        # Per-run aggregates. Phase 1-2 ``self._rows`` view is preserved
        # as a flat list across all runs — see ``all()`` / ``__len__``.
        self._rows: list[StoredSignal] = []
        self._runs: dict[str, RunMetadata] = {}
        self._run_signals: dict[str, list[StoredSignal]] = {}
        self._run_errors: dict[str, list[ErrorRecord]] = {}
        self._active_run_id: str | None = None
        self._strict = strict_run_lifecycle
        self._closed = False

    def _check_open(self) -> None:
        """Raise ``RuntimeError`` if the store has been closed.

        Symmetric with ``SqliteBacktestStore._check_open()``: callers can
        treat the two stores interchangeably, including the post-close
        contract that any operation raises.
        """
        if self._closed:
            msg = (
                "BacktestStore is closed. "
                "Create a new store to continue."
            )
            raise RuntimeError(msg)

    @property
    def schema_version(self) -> int | None:
        """In-memory store has no persistent schema; returns ``None``."""
        return None

    @property
    def strict_run_lifecycle(self) -> bool:
        """Whether this store rejects implicit runs (read-only)."""
        return self._strict

    @property
    def active_run_id(self) -> str | None:
        """The ``run_id`` of the active run, or ``None``."""
        self._check_open()
        return self._active_run_id

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
        """Open a new run; auto-finishes any currently-active run first.

        ``run_id`` defaults to a fresh uuid4 hex string. Callers can
        pass an explicit value for deterministic test fixtures or for
        the 4-cell runner that wants e.g. ``"cell_2_tier1_fusion"``.
        """
        self._check_open()
        if self._active_run_id is not None:
            self.finish_run()
        rid = run_id if run_id is not None else uuid.uuid4().hex
        meta = RunMetadata(
            run_id=rid,
            started_at=datetime.now(UTC),
            finished_at=None,
            profile_id=profile.profile_id,
            profile_content_hash=profile.content_hash(),
            universe_id=universe_id,
            source_config_hash=source_config_hash,
            total_signals_processed=0,
            total_errors=0,
            dataset_window_start=dataset_window_start,
            dataset_window_end=dataset_window_end,
            notes=notes,
        )
        self._runs[rid] = meta
        self._run_signals[rid] = []
        self._run_errors[rid] = []
        self._active_run_id = rid
        return rid

    def _ensure_active_run(self) -> str:
        """Resolve the run_id for the next write.

        Permissive: auto-create an implicit run on first call.
        Strict: raise if no run was started explicitly.
        """
        if self._active_run_id is not None:
            return self._active_run_id
        if self._strict:
            msg = (
                "BacktestStore was opened with strict_run_lifecycle=True; "
                "call start_run() before add() or record_error()."
            )
            raise RunLifecycleError(msg)
        # Permissive auto-implicit run. Synthesise a minimal RunMetadata
        # without a profile (Phase 1-2 callers don't pass one). The
        # profile_id placeholder marks it as auto-created.
        meta = RunMetadata(
            run_id=self._IMPLICIT_RUN_ID,
            started_at=datetime.now(UTC),
            finished_at=None,
            profile_id="<implicit>",
            profile_content_hash="<implicit>",
            universe_id=None,
            source_config_hash=None,
            total_signals_processed=0,
            total_errors=0,
            dataset_window_start=None,
            dataset_window_end=None,
            notes="Auto-created by permissive store on first add().",
        )
        self._runs[self._IMPLICIT_RUN_ID] = meta
        self._run_signals[self._IMPLICIT_RUN_ID] = []
        self._run_errors[self._IMPLICIT_RUN_ID] = []
        self._active_run_id = self._IMPLICIT_RUN_ID
        return self._IMPLICIT_RUN_ID

    def add(
        self,
        event: EnrichedEvent,
        decision: LabelDecision,
        size: PositionSize,
        *,
        pipeline_latency_ms: float | None = None,
        data_source_latency_ms: float | None = None,
    ) -> StoredSignal:
        """Persist one fully-decided signal to the active run.

        Phase 3.2.1: gains keyword-only ``pipeline_latency_ms`` and
        ``data_source_latency_ms`` so the metric calculator (3.2.3)
        can flag stale-by-design pipelines. Defaults to ``None`` so
        Phase 1-2 callers ignore them.
        """
        self._check_open()
        rid = self._ensure_active_run()
        pr = event.print_
        moneyness = pr.spot_price / pr.strike if pr.strike > 0 else Decimal(0)

        row = StoredSignal(
            timestamp=pr.timestamp,
            ticker=pr.ticker,
            option_type=pr.option_type,
            strike=pr.strike,
            expiry=pr.expiry,
            dte=pr.dte,
            premium=pr.premium_paid,
            option_price=pr.option_price,
            moneyness=moneyness,
            uoa_score=event.uoa_score,
            convexity_score=event.convexity_score,
            event_score=event.event_score,
            gamma_score=event.gamma_score,
            price_confirmation_score=event.price_confirmation_score,
            sector_confirmation_score=event.sector_confirmation_score,
            time_of_day_weight=event.time_of_day_weight,
            cluster_density_score=event.cluster_density_score,
            relative_premium_score=event.relative_premium_score,
            dte_multiplier_applied=event.dte_multiplier_applied,
            combined_score_pre_penalty=event.combined_score_pre_penalty,
            combined_score_post_penalty=event.combined_score_post_penalty,
            penalties_applied=list(event.applied_penalties),
            contradiction_penalty_applied=event.contradiction_penalty_applied,
            sweep_classification=event.sweep_classification,
            is_iso=pr.is_iso,
            gamma_flag=event.is_gamma_acceleration,
            sector_confirmation=event.has_sector_confirmation,
            dark_pool_confirmation=event.has_dark_pool_confirmation,
            label=decision.label,
            label_reason=decision.reason,
            max_r=size.max_r,
            scale_in=size.scale_in,
            initial_r=size.initial_r,
            next_day_oi_confirmed=event.next_day_oi_confirmed,
            opening_closing_score=event.opening_closing_score,
            m28_confirmation_score=event.m28_confirmation_score,
            run_id=rid,
            event_id=pr.event_id,
            pipeline_latency_ms=pipeline_latency_ms,
            data_source_latency_ms=data_source_latency_ms,
        )
        self._rows.append(row)
        self._run_signals[rid].append(row)
        # Bump the run's signal counter via model_copy (RunMetadata is frozen).
        old = self._runs[rid]
        self._runs[rid] = old.model_copy(
            update={"total_signals_processed": old.total_signals_processed + 1},
        )
        return row

    def record_error(
        self,
        *,
        stage_name: str,
        error_type: str,
        error_message: str,
        event_id: str | None = None,
    ) -> ErrorRecord:
        """Append a stage-error record to the active run.

        Bumps ``RunMetadata.total_errors`` for that run.
        """
        self._check_open()
        rid = self._ensure_active_run()
        rec = ErrorRecord(
            run_id=rid,
            event_id=event_id,
            stage_name=stage_name,
            error_type=error_type,
            error_message=error_message,
            occurred_at=datetime.now(UTC),
        )
        self._run_errors[rid].append(rec)
        old = self._runs[rid]
        self._runs[rid] = old.model_copy(
            update={"total_errors": old.total_errors + 1},
        )
        return rec

    def finish_run(self) -> RunMetadata | None:
        """End the active run; returns its final metadata or ``None``.

        Does **not** close the store. After this returns, the caller can
        ``start_run()`` again on the same store. This is the run-level
        lifecycle method; ``close()`` is the store-level one. Phase
        3.2.4's 4-cell runner uses one store across four
        ``finish_run()`` calls and a single trailing ``close()``.

        Idempotent in permissive mode (no-op when no run active).
        Strict mode raises ``RunLifecycleError`` on no-active-run.
        """
        self._check_open()
        if self._active_run_id is None:
            if self._strict:
                msg = "finish_run() called with no active run in strict mode."
                raise RunLifecycleError(msg)
            return None
        rid = self._active_run_id
        old = self._runs[rid]
        self._runs[rid] = old.model_copy(update={"finished_at": datetime.now(UTC)})
        self._active_run_id = None
        return self._runs[rid]

    def get_run(self, run_id: str) -> RunMetadata | None:
        self._check_open()
        return self._runs.get(run_id)

    def list_runs(self) -> list[RunMetadata]:
        # Sort by started_at desc (most recent first). Stable sort retains
        # insertion order on ties — irrelevant in practice but defensive.
        self._check_open()
        return sorted(
            self._runs.values(), key=lambda r: r.started_at, reverse=True,
        )

    def iter_records(self, run_id: str) -> Iterator[StoredSignal]:
        """Stream signals belonging to ``run_id`` in insertion order.

        Returns an empty iterator if the run is unknown — silent rather
        than raising, to match how SQLite would behave for a missing
        run.
        """
        self._check_open()
        yield from self._run_signals.get(run_id, [])

    def iter_errors(self, run_id: str) -> Iterator[ErrorRecord]:
        self._check_open()
        yield from self._run_errors.get(run_id, [])

    # Allowed score_name values for update_signal_score (Phase 3.4.8).
    # Restricted to float-typed sub-score fields. Adding a new sub-score
    # field requires bumping this set.
    _UPDATABLE_SCORE_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "uoa_score",
        "convexity_score",
        "event_score",
        "gamma_score",
        "price_confirmation_score",
        "sector_confirmation_score",
        "time_of_day_weight",
        "cluster_density_score",
        "relative_premium_score",
        "dte_multiplier_applied",
        "opening_closing_score",
        "m28_confirmation_score",
        "combined_score_pre_penalty",
        "combined_score_post_penalty",
    })

    def update_signal_score(
        self,
        run_id: str,
        event_id: str,
        score_name: str,
        value: float | None,
    ) -> bool:
        """Update one float sub-score on a stored signal.

        Phase 3.4.8 BREAKING CHANGE — see protocol docstring.
        """
        self._check_open()
        if score_name not in self._UPDATABLE_SCORE_FIELDS:
            msg = (
                f"update_signal_score: score_name '{score_name}' "
                f"is not in the allowed set "
                f"{sorted(self._UPDATABLE_SCORE_FIELDS)}"
            )
            raise ValueError(msg)
        rows = self._run_signals.get(run_id, [])
        for i, row in enumerate(rows):
            if row.event_id == event_id:
                updated = row.model_copy(update={score_name: value})
                rows[i] = updated
                # Mirror in self._rows
                for j, r in enumerate(self._rows):
                    if r.run_id == run_id and r.event_id == event_id:
                        self._rows[j] = updated
                        break
                return True
        return False

    def close(self) -> None:
        """Mark the store closed; finish any active run first.

        Symmetric with the SQLite store's ``close()`` so callers can
        treat the two interchangeably. Distinct from ``finish_run()``:
        ``finish_run()`` ends a run but the store remains usable;
        ``close()`` makes any further operation raise ``RuntimeError``.

        Idempotent: closing twice is a no-op (does not raise).
        """
        if self._closed:
            return
        if self._active_run_id is not None:
            self.finish_run()
        self._closed = True

    # ---- Phase 1-2 surface preserved ------------------------------------

    def all(self) -> Sequence[StoredSignal]:
        """Return all stored signals across every run (Phase 1-2 surface).

        Order: insertion order. Used by Phase 1-2 tests that expected
        ``store.all()`` to enumerate every signal flat.
        """
        self._check_open()
        return tuple(self._rows)

    def __len__(self) -> int:
        # __len__ stays guard-free: ``len(store)`` is used by tests in
        # ways where a closed-state RuntimeError would be a surprise.
        # Reading length of a closed store returns the snapshot count.
        return len(self._rows)

    def to_dataframe(self) -> pd.DataFrame:
        """Return the store contents as a pandas DataFrame for analysis.

        Lists/structs are flattened where practical; ``penalties_applied`` is
        kept as a list-of-dict column.
        """
        self._check_open()
        records: list[dict[str, object]] = []
        for r in self._rows:
            d = r.model_dump()
            d["penalties_applied"] = [p.model_dump() for p in r.penalties_applied]
            records.append(d)
        return pd.DataFrame.from_records(records)
