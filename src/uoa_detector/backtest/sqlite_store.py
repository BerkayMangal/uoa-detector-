"""``SqliteBacktestStore`` — persistent backtest store backed by SQLite.

Implements ``BacktestStoreProtocol``. Migration handled by Alembic at
open time (auto-upgrade-to-head), schema versioning surfaced through
``schema_version``.

Design rationale:

  * **WAL journal mode.** ``PRAGMA journal_mode=WAL`` is set at open;
    enables concurrent reads alongside the write path. SQLite's
    default rollback journal locks the whole database for any write,
    which serialises the metric calculator behind the orchestrator.

  * **Batched writes.** ``add()`` and ``record_error()`` buffer rows
    in memory; ``finish_run()`` flushes. Default buffer size is 100
    rows. A pending flush is also forced on ``start_run()`` so the
    previous run's data is durable before the next run starts.

  * **Idempotent open.** Re-opening an existing database calls Alembic
    to upgrade to head. If the database is already at head, that is a
    no-op. If it is at an older revision, the upgrade runs and the
    open succeeds. If it is at a NEWER revision than this code
    knows about, Alembic raises and we propagate — running newer code
    against an older database is fine, running older code against a
    newer database is not.

  * **Append-only at the API surface.** No ``update()`` or ``delete()``
    methods exist on the public surface. The only mutations are
    field-level: ``RunMetadata.finished_at`` set by ``finish_run()``,
    ``RunMetadata.total_signals_processed`` and
    ``RunMetadata.total_errors`` incremented as rows arrive. This is
    enforced by tests (``test_no_update_method_on_signal_table``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from uoa_detector.backtest.models import ErrorRecord, RunMetadata
from uoa_detector.backtest.sqlite_models import (
    BacktestRunErrorRow,
    BacktestRunRow,
    Base,
    SignalRow,
)
from uoa_detector.backtest.store import StoredSignal
from uoa_detector.errors import RunLifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

    from uoa_detector.calibration import CalibrationProfile
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.domain.labels import LabelDecision
    from uoa_detector.domain.risk import PositionSize


_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _make_alembic_config(database_url: str) -> AlembicConfig:
    """Build an in-memory Alembic config pointed at the package's migrations.

    No filesystem ``alembic.ini`` — the config is fully programmatic so
    the package can be installed and run from anywhere. ``script_location``
    must be a filesystem path (Alembic doesn't accept dotted package
    names here); we resolve it relative to this module's location so
    it works whether the package is installed editable or as a wheel.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


class SqliteBacktestStore:
    """Persistent ``BacktestStoreProtocol`` implementation."""

    _DEFAULT_FLUSH_THRESHOLD: ClassVar[int] = 100

    def __init__(
        self,
        database_url: str,
        *,
        strict_run_lifecycle: bool = False,
        flush_threshold: int | None = None,
    ) -> None:
        """Open or create a SQLite database at ``database_url``.

        ``database_url`` examples:
          - ``sqlite:///./backtest.db`` (file, relative path)
          - ``sqlite:////absolute/path/to/backtest.db`` (file, absolute path)
          - ``sqlite:///:memory:`` (in-process memory; loses data on close)

        On open the database is migrated to Alembic head. The journal
        mode is set to WAL for file-backed databases (no-op for memory
        databases — WAL is unavailable there).
        """
        self._database_url = database_url
        self._engine: Engine = create_engine(database_url, future=True)
        self._strict = strict_run_lifecycle
        self._flush_threshold = flush_threshold or self._DEFAULT_FLUSH_THRESHOLD

        # Set WAL mode at connection open. This applies to every
        # connection from the pool, not just the first. SQLite-only:
        # PRAGMA is not valid on Postgres, so guard by dialect (this
        # store also backs the live Postgres sink — Phase 4).
        if self._engine.dialect.name == "sqlite":
            @event.listens_for(self._engine, "connect")
            def _set_pragmas(dbapi_conn: object, _conn_record: object) -> None:
                cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()

        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False, future=True,
        )

        # Migrate to head. This creates the tables on a fresh database
        # and is a no-op on an up-to-date one.
        self._migrate_to_head()

        self._active_run_id: str | None = None
        self._signal_buffer: list[SignalRow] = []
        self._error_buffer: list[BacktestRunErrorRow] = []
        self._closed = False

    # ---- Lifecycle / migration -----------------------------------------

    def _check_open(self) -> None:
        """Raise ``RuntimeError`` if the store has been closed.

        Phase 3.2.1 lifecycle separation: ``finish_run()`` ends a run
        but leaves the engine alive for the next ``start_run()``;
        ``close()`` disposes the engine and the store cannot be used
        further. Every public operation checks this guard.
        """
        if self._closed:
            msg = (
                "SqliteBacktestStore is closed. "
                "close() disposes the engine; create a new store to continue."
            )
            raise RuntimeError(msg)

    def _migrate_to_head(self) -> None:
        # Alembic migration scripts are authored against SQLite. For other
        # dialects (Postgres, the Phase 4 live sink) create the schema
        # directly from the ORM metadata — idempotent, so it is a no-op
        # when the tables already exist.
        if self._engine.dialect.name != "sqlite":
            Base.metadata.create_all(self._engine)
            return
        cfg = _make_alembic_config(self._database_url)
        command.upgrade(cfg, "head")

    @property
    def schema_version(self) -> int | None:
        """Returns 1 on v1 schema; climbs as migrations land.

        We translate Alembic's revision string to an integer by reading
        the migration filenames' numeric prefix (``0001_initial`` → 1).
        Tests assert ``>= 1``; the integer is informative, not the
        identity used for migrations themselves.
        """
        self._check_open()
        with self._engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            rev = ctx.get_current_revision()
        if rev is None:
            return None
        # Revision ids are "0001_initial", "0002_xyz", etc. Take the
        # leading numeric token.
        leading = rev.split("_", 1)[0]
        return int(leading) if leading.isdigit() else 1

    @property
    def strict_run_lifecycle(self) -> bool:
        return self._strict

    @property
    def active_run_id(self) -> str | None:
        self._check_open()
        return self._active_run_id

    # ---- Run lifecycle -------------------------------------------------

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
        """Open a new run; auto-finishes any currently-active run first."""
        self._check_open()
        if self._active_run_id is not None:
            self.finish_run()
        rid = run_id if run_id is not None else uuid.uuid4().hex

        with self._session_factory() as session:
            row = BacktestRunRow(
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
            session.add(row)
            session.commit()

        self._active_run_id = rid
        return rid

    def _ensure_active_run(self) -> str:
        """Resolve or auto-create the active run_id."""
        if self._active_run_id is not None:
            return self._active_run_id
        if self._strict:
            msg = (
                "SqliteBacktestStore was opened with strict_run_lifecycle=True; "
                "call start_run() before add() or record_error()."
            )
            raise RunLifecycleError(msg)

        # Auto-implicit run. Since we don't have a profile to hash, use
        # a placeholder and create the run row directly.
        rid = "implicit-default"
        with self._session_factory() as session:
            existing = session.get(BacktestRunRow, rid)
            if existing is None:
                row = BacktestRunRow(
                    run_id=rid,
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
                session.add(row)
                session.commit()
        self._active_run_id = rid
        return rid

    def finish_run(self) -> RunMetadata | None:
        """Flush pending writes and mark the active run finished.

        Does **not** close the store. After this returns, the caller can
        ``start_run()`` again and reuse the same engine. This is the
        run-level lifecycle method; ``close()`` is the store-level one.
        Phase 3.2.4's 4-cell runner uses one store across four
        ``finish_run()`` calls and a single trailing ``close()``.
        """
        self._check_open()
        if self._active_run_id is None:
            if self._strict:
                msg = "finish_run() called with no active run in strict mode."
                raise RunLifecycleError(msg)
            return None
        rid = self._active_run_id

        self._flush_buffers()

        with self._session_factory() as session:
            row = session.get(BacktestRunRow, rid)
            if row is None:
                self._active_run_id = None
                return None
            row.finished_at = datetime.now(UTC)
            session.commit()
            meta = self._row_to_metadata(row)

        self._active_run_id = None
        return meta

    # ---- Writes --------------------------------------------------------

    def add(
        self,
        event: EnrichedEvent,
        decision: LabelDecision,
        size: PositionSize,
        *,
        pipeline_latency_ms: float | None = None,
        data_source_latency_ms: float | None = None,
    ) -> StoredSignal:
        """Buffer one signal; flush if the buffer hit the threshold."""
        self._check_open()
        rid = self._ensure_active_run()
        pr = event.print_
        moneyness = pr.spot_price / pr.strike if pr.strike > 0 else Decimal(0)

        # Build the in-memory StoredSignal so callers get a typed return,
        # even before the row is flushed to disk.
        stored = StoredSignal(
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

        # Build the ORM row separately. The full record is JSON-encoded
        # for fidelity; top-level columns are the cross-cell-comparison
        # axes.
        full_record_json = stored.model_dump_json()
        # We need to grab profile_id + hash from the active run, since
        # they're not on the StoredSignal directly.
        with self._session_factory() as session:
            run_row = session.get(BacktestRunRow, rid)
            assert run_row is not None
            profile_id = run_row.profile_id
            profile_content_hash = run_row.profile_content_hash

        signal_row = SignalRow(
            run_id=rid,
            event_id=pr.event_id,
            ticker=pr.ticker,
            ts=pr.timestamp,
            label=decision.label.value,
            max_r=size.max_r,
            combined_score_pre=event.combined_score_pre_penalty,
            combined_score_post=event.combined_score_post_penalty,
            profile_id=profile_id,
            profile_content_hash=profile_content_hash,
            pipeline_latency_ms=pipeline_latency_ms,
            data_source_latency_ms=data_source_latency_ms,
            full_record_json=full_record_json,
        )
        self._signal_buffer.append(signal_row)
        if len(self._signal_buffer) >= self._flush_threshold:
            self._flush_buffers()

        return stored

    def record_error(
        self,
        *,
        stage_name: str,
        error_type: str,
        error_message: str,
        event_id: str | None = None,
    ) -> ErrorRecord:
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
        err_row = BacktestRunErrorRow(
            run_id=rid,
            event_id=event_id,
            stage_name=stage_name,
            error_type=error_type,
            error_message=error_message,
            occurred_at=rec.occurred_at,
        )
        self._error_buffer.append(err_row)
        if len(self._error_buffer) >= self._flush_threshold:
            self._flush_buffers()
        return rec

    def _flush_buffers(self) -> None:
        """Persist buffered signals + errors and bump per-run counters."""
        if not self._signal_buffer and not self._error_buffer:
            return

        signals_by_run: dict[str, int] = {}
        errors_by_run: dict[str, int] = {}
        for sig in self._signal_buffer:
            signals_by_run[sig.run_id] = signals_by_run.get(sig.run_id, 0) + 1
        for err in self._error_buffer:
            errors_by_run[err.run_id] = errors_by_run.get(err.run_id, 0) + 1

        with self._session_factory() as session:
            session.add_all(self._signal_buffer)
            session.add_all(self._error_buffer)
            for rid, count in signals_by_run.items():
                run = session.get(BacktestRunRow, rid)
                if run is not None:
                    run.total_signals_processed += count
            for rid, count in errors_by_run.items():
                run = session.get(BacktestRunRow, rid)
                if run is not None:
                    run.total_errors += count
            session.commit()

        self._signal_buffer = []
        self._error_buffer = []

    # ---- Reads ---------------------------------------------------------

    def get_run(self, run_id: str) -> RunMetadata | None:
        # Force a flush so any in-memory writes are visible.
        self._check_open()
        self._flush_buffers()
        with self._session_factory() as session:
            row = session.get(BacktestRunRow, run_id)
            return self._row_to_metadata(row) if row is not None else None

    def list_runs(self) -> list[RunMetadata]:
        self._check_open()
        self._flush_buffers()
        with self._session_factory() as session:
            stmt = select(BacktestRunRow).order_by(BacktestRunRow.started_at.desc())
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_metadata(r) for r in rows]

    def iter_records(self, run_id: str) -> Iterator[StoredSignal]:
        """Stream signals belonging to ``run_id`` in event-time order.

        Uses a yielded SQLAlchemy stream so multi-million-row runs do
        not have to fit in RAM. Signals are reconstructed from the
        ``full_record_json`` blob, restoring full fidelity.
        """
        self._check_open()
        self._flush_buffers()
        with self._session_factory() as session:
            stmt = (
                select(SignalRow)
                .where(SignalRow.run_id == run_id)
                .order_by(SignalRow.ts)
            )
            for row in session.execute(stmt).scalars():
                yield StoredSignal.model_validate_json(row.full_record_json)

    def iter_errors(self, run_id: str) -> Iterator[ErrorRecord]:
        self._check_open()
        self._flush_buffers()
        with self._session_factory() as session:
            stmt = (
                select(BacktestRunErrorRow)
                .where(BacktestRunErrorRow.run_id == run_id)
                .order_by(BacktestRunErrorRow.occurred_at)
            )
            for row in session.execute(stmt).scalars():
                yield ErrorRecord(
                    run_id=row.run_id,
                    event_id=row.event_id,
                    stage_name=row.stage_name,
                    error_type=row.error_type,
                    error_message=row.error_message,
                    occurred_at=row.occurred_at,
                )

    # Allowed score_name values for update_signal_score (Phase 3.4.8).
    # Must match the in-memory store's whitelist exactly.
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

        Implementation note: signal data lives BOTH in dedicated SQL
        columns (combined_score_pre, etc.) AND in full_record_json.
        The JSON blob is the source of truth for re-deserialization
        via iter_records, so we update both:
          1. Deserialize full_record_json → StoredSignal
          2. Replace the field via model_copy
          3. Re-serialize and write back
          4. Also UPDATE the SQL column if score_name has a
             dedicated column (currently only combined_score_pre /
             combined_score_post)
        """
        self._check_open()
        if score_name not in self._UPDATABLE_SCORE_FIELDS:
            msg = (
                f"update_signal_score: score_name '{score_name}' "
                f"is not in the allowed set "
                f"{sorted(self._UPDATABLE_SCORE_FIELDS)}"
            )
            raise ValueError(msg)
        # Flush buffered rows so the row is queryable
        self._flush_buffers()
        with self._session_factory() as session:
            stmt = select(SignalRow).where(
                SignalRow.run_id == run_id,
                SignalRow.event_id == event_id,
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return False
            stored = StoredSignal.model_validate_json(row.full_record_json)
            updated = stored.model_copy(update={score_name: value})
            row.full_record_json = updated.model_dump_json()
            # Sync dedicated SQL columns when the field has one
            if score_name == "combined_score_pre_penalty":
                row.combined_score_pre = value
            elif score_name == "combined_score_post_penalty":
                row.combined_score_post = value
            session.commit()
            return True

    # ---- Helpers -------------------------------------------------------

    @staticmethod
    def _row_to_metadata(row: BacktestRunRow) -> RunMetadata:
        return RunMetadata(
            run_id=row.run_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
            profile_id=row.profile_id,
            profile_content_hash=row.profile_content_hash,
            universe_id=row.universe_id,
            source_config_hash=row.source_config_hash,
            total_signals_processed=row.total_signals_processed,
            total_errors=row.total_errors,
            dataset_window_start=row.dataset_window_start,
            dataset_window_end=row.dataset_window_end,
            notes=row.notes,
        )

    def close(self) -> None:
        """Flush, finalise, and dispose. After this the store is unusable.

        Distinct from ``finish_run()``: ``finish_run()`` ends a run but
        leaves the engine alive for the next ``start_run()``; ``close()``
        disposes the engine and marks the store closed. Subsequent calls
        to any public method raise ``RuntimeError``.

        Idempotent: calling ``close()`` on an already-closed store is a
        no-op (does not raise). This makes ``with`` and ``try/finally``
        wrappers safe.
        """
        if self._closed:
            return
        if self._active_run_id is not None:
            self.finish_run()
        else:
            self._flush_buffers()
        self._engine.dispose()
        self._closed = True

    def __enter__(self) -> SqliteBacktestStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
