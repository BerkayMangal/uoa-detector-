"""Every signal row of a run, for the Alfa Board (Phase 5.2.A1).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Render") and §5 A1.

- Aggregation covers the whole selected run: every ``signal`` row, with no
  score-ordered cap.
- Parsed rows are cached in process, keyed by ``(run_id, event_id)``. A render
  asks the database only for the run's event ids and parses the rows it has
  not seen before. Signal rows are append-only on the live path.
- Each print is joined to its ``alfa_print_meta`` row (fill side and option
  chain) when the worker wrote one. The meta row is written just after the
  signal row, so a print read without meta is looked up again on the next
  render until its meta appears. Legacy rows never get one.
- Unparseable rows are skipped with one WARNING and remembered, so they are
  not re-parsed on every render.
- The cache keeps the requested run and the newest cached ``live-*`` run.
  Other runs are evicted, which bounds memory in a long-running process
  without a size constant.

No Unusual Whales call is made here. The module reads the database only.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.backtest.store import StoredSignal
from webapp.board.db import make_engine, session_factory
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

_logger = logging.getLogger(__name__)
_LIVE_PREFIX = "live-"


def live_run_tips(engine: Engine) -> list[tuple[datetime, str]]:
    """``(newest print time, run_id)`` for every ``live-*`` run, newest print first.

    The single source of "which live run is current" for the refresher and the
    daily-job clock: the live worker keeps writing into the run id it started
    with across a UTC midnight, so a run id is never derived from the calendar
    date (review RT-2).
    """
    stmt = (
        select(SignalRow.run_id, func.max(SignalRow.ts))
        .where(SignalRow.run_id.like(f"{_LIVE_PREFIX}%"))
        .group_by(SignalRow.run_id)
    )
    with Session(engine) as session:
        tips = [
            (_as_utc(ts), run_id)
            for run_id, ts in session.execute(stmt)
            if isinstance(ts, datetime)
        ]
    return sorted(tips, reverse=True)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class PrintMetaView:
    """The board's slice of an ``alfa_print_meta`` row."""

    fill_side: str
    option_chain: str | None


@dataclass(frozen=True)
class BoardPrint:
    """One stored signal of a run, with its print meta when it exists."""

    run_id: str
    event_id: str
    signal: StoredSignal
    meta: PrintMetaView | None


@dataclass
class _RunCache:
    signals: dict[str, StoredSignal] = field(default_factory=dict)
    unparseable: set[str] = field(default_factory=set)
    meta: dict[str, PrintMetaView] = field(default_factory=dict)


class BoardSignalReader:
    """Loads all prints of a run from ``signal`` plus ``alfa_print_meta``."""

    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        self._owns_engine = engine is None
        self._engine = engine if engine is not None else make_engine(database_url)
        self._sessions = session_factory(self._engine)
        self._lock = threading.Lock()
        self._runs: dict[str, _RunCache] = {}
        self._tables_ready = False
        self.parse_attempts = 0  # cumulative JSON parses, for tests and diagnostics

    @property
    def engine(self) -> Engine:
        return self._engine

    def cached_runs(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._runs)

    def load_run(self, run_id: str) -> list[BoardPrint]:
        """Every parseable print of ``run_id``, oldest first."""
        with self._lock:
            if not self._tables_ready:
                ensure_telemetry_tables(self._engine)
                self._tables_ready = True
            cache = self._runs.setdefault(run_id, _RunCache())
            self._evict(keep=run_id)
            with self._sessions() as session:
                keys: Sequence[str] = session.execute(
                    select(SignalRow.event_id)
                    .where(SignalRow.run_id == run_id)
                    .order_by(SignalRow.ts, SignalRow.event_id),
                ).scalars().all()
                self._parse_new_rows(session, run_id, cache, keys)
                self._join_new_meta(session, run_id, cache, keys)
            return [
                BoardPrint(
                    run_id=run_id,
                    event_id=key,
                    signal=cache.signals[key],
                    meta=cache.meta.get(key),
                )
                for key in keys
                if key in cache.signals
            ]

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()

    def _evict(self, *, keep: str) -> None:
        live_runs = [rid for rid in self._runs if rid.startswith(_LIVE_PREFIX)]
        newest_live = max(live_runs) if live_runs else None
        for rid in list(self._runs):
            if rid not in (keep, newest_live):
                del self._runs[rid]

    def _parse_new_rows(
        self, session: Session, run_id: str, cache: _RunCache, keys: Sequence[str],
    ) -> None:
        missing = [k for k in keys if k not in cache.signals and k not in cache.unparseable]
        if not missing:
            return
        stmt = select(SignalRow.event_id, SignalRow.full_record_json).where(
            SignalRow.run_id == run_id,
        )
        if cache.signals or cache.unparseable:
            stmt = stmt.where(SignalRow.event_id.in_(missing))
        for event_id, raw in session.execute(stmt):
            if event_id in cache.signals or event_id in cache.unparseable:
                continue
            self.parse_attempts += 1
            try:
                cache.signals[event_id] = StoredSignal.model_validate_json(raw)
            except Exception:
                cache.unparseable.add(event_id)
                _logger.warning("alfa board: skipping unparseable signal %s/%s", run_id, event_id)

    def _join_new_meta(
        self, session: Session, run_id: str, cache: _RunCache, keys: Sequence[str],
    ) -> None:
        missing = [k for k in keys if k not in cache.meta]
        if not missing:
            return
        # One query, not two. A probe used to run first, selecting EVERY event_id
        # of the run to decide which of ``missing`` were worth asking for — a full
        # scan of the run's print-meta on every board render whose only effect was
        # to narrow an IN list that narrows nothing: an id with no row returns no
        # row either way, and the cache is filled from what comes back rather than
        # from what was asked for (audit 2026-09-19).
        stmt = select(
            AlfaPrintMeta.event_id, AlfaPrintMeta.fill_side, AlfaPrintMeta.option_chain,
        ).where(AlfaPrintMeta.run_id == run_id)
        if cache.meta:
            # First load takes the run in one sweep; later renders ask only for
            # what they are missing, which keeps the IN list bounded by the page.
            stmt = stmt.where(AlfaPrintMeta.event_id.in_(missing))
        for event_id, fill_side, option_chain in session.execute(stmt):
            cache.meta[event_id] = PrintMetaView(fill_side=fill_side, option_chain=option_chain)
