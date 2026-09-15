"""Alfa Board stage telemetry and print metadata (Phase 5.2.A0c).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1, §4.2 and §5 A3;
decisions P7 (side tables) and P11 (side-aware direction).

The live worker's ``Pipeline`` builds a ``SignalDecisionRecord`` for every
event and used to discard it (``orchestrator.py:250-258``). This module keeps
the parts the board needs in two append-only side tables:

- ``alfa_stage_telemetry``: one row per (run, event, stage). It holds the
  stage's ``branch`` string, its full telemetry metadata as JSON, and a
  ``degraded`` flag.
- ``alfa_print_meta``: one row per (run, event). It holds the aggressor side
  (``fill_side``), the UW option chain and the print identity.

Why side tables (decision P7): ``StoredSignal`` is ``extra="forbid"`` on main
and on ``phase-3-final``. A new key, even a null one, would make a rolled-back
dashboard drop every new row. Side tables keep a rollback data-safe and leave
every frozen stage and model untouched.

Degraded detection. On the live worker, M21, M22, M24, M25, M26 and M27 read
their providers through ``Degrading*`` wrappers
(``build_live_stage_pipeline(..., degrade_transient_errors=True)``). A wrapper
turns a rate limit, a daily-quota 429, a transient error or an open breaker
into that method's no-data return, so the stage's branch string reads like
real missing data. Each wrapper has a public cumulative ``errors`` counter.
The worker processes events one at a time, so the writer diffs every
wrapper's counter against the previous record: a stage whose wrapper counted
a new error during this event is written ``degraded = True``. M22 and M24
share one catalyst wrapper, so a catalyst error marks both. That is the safe
side: the board then shows "unknown", never "clean".

Option chain. ``UnusualWhalesFlowPollSource`` builds event ids as
``uw-{ticker}-{option_chain}-{created_at}``. The chain is taken from the event
id only when it encodes the print's own expiry, type and strike; otherwise it
is stored as null. Nothing is guessed.

Safety. The writer runs inside the live pipeline. It never raises: a database
error, a parse error or a missing run id is logged as one WARNING and the
event is dropped from these tables. Live signal ingestion (the ``signal``
table) is never affected.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final, Protocol, cast

from sqlalchemy import Boolean, Date, DateTime, String, Table, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from uoa_detector.sources.unusual_whales.providers.degrading import (
    DegradingCatalystCalendarProvider,
    DegradingDarkPoolPrintProvider,
    DegradingDealerPositioningProvider,
    DegradingIVHistoryProvider,
    DegradingOpenInterestProvider,
    DegradingPeerFlowProvider,
    DegradingSectorMapProvider,
)
from webapp.board.db import AlfaBase, make_engine, session_factory

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from uoa_detector.domain.events import OptionsPrint
    from uoa_detector.observability.decision_record import SignalDecisionRecord

_logger = logging.getLogger(__name__)

# Every public degrading wrapper class. A stage attribute holding one of these
# is the provider path whose errors must mark that stage degraded.
_DEGRADING_TYPES = (
    DegradingCatalystCalendarProvider,
    DegradingDarkPoolPrintProvider,
    DegradingDealerPositioningProvider,
    DegradingIVHistoryProvider,
    DegradingOpenInterestProvider,
    DegradingPeerFlowProvider,
    DegradingSectorMapProvider,
)

# An OCC-style option symbol: root, YYMMDD, C or P, strike x 1000 in 8 digits.
_OCC_IN_TEXT: Final = re.compile(r"(?<![A-Z0-9.])([A-Z][A-Z0-9.]*)(\d{6})([CP])(\d{8})(?!\d)")
_STRIKE_SCALE: Final = Decimal(1000)


class AlfaStageTelemetry(AlfaBase):
    """``alfa_stage_telemetry``: one append-only row per (run, event, stage)."""

    __tablename__ = "alfa_stage_telemetry"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_name: Mapped[str] = mapped_column(String, primary_key=True)
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    profile_content_hash: Mapped[str] = mapped_column(String, nullable=False)
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlfaPrintMeta(AlfaBase):
    """``alfa_print_meta``: one append-only row per (run, event)."""

    __tablename__ = "alfa_print_meta"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    option_chain: Mapped[str | None] = mapped_column(String, nullable=True)
    fill_side: Mapped[str] = mapped_column(String, nullable=False)
    option_type: Mapped[str] = mapped_column(String, nullable=False)
    strike: Mapped[str] = mapped_column(String, nullable=False)  # exact Decimal text
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    print_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def ensure_telemetry_tables(engine: Engine) -> None:
    """Create both telemetry tables if missing. Never alters or drops anything."""
    for model in (AlfaStageTelemetry, AlfaPrintMeta):
        cast("Table", model.__table__).create(engine, checkfirst=True)


class ErrorCounter(Protocol):
    """What the writer reads from a degrading wrapper: its cumulative error count."""

    errors: int


def degrading_wrappers_by_stage(stages: Iterable[object]) -> dict[str, tuple[ErrorCounter, ...]]:
    """Map each stage name to the degrading wrappers held by that stage instance.

    On ``build_live_stage_pipeline(..., degrade_transient_errors=True)`` this
    yields M21 dealer gamma, M22 catalyst, M24 IV history and catalyst (the
    same catalyst instance as M22), M25 sector map and peer flow, M26 dark
    pool and M27 open interest. Stages without a wrapper are absent.
    """
    found: dict[str, tuple[ErrorCounter, ...]] = {}
    for stage in stages:
        name = getattr(stage, "name", None)
        attributes = getattr(stage, "__dict__", None)
        if not isinstance(name, str) or not isinstance(attributes, dict):
            continue
        wrappers: list[ErrorCounter] = []
        for value in attributes.values():
            if isinstance(value, _DEGRADING_TYPES) and all(value is not w for w in wrappers):
                wrappers.append(value)
        if wrappers:
            found[name] = tuple(wrappers)
    return found


class _DegradeTracker:
    """Diffs wrapper error counters between consecutive records."""

    def __init__(self, by_stage: Mapping[str, Sequence[ErrorCounter]]) -> None:
        self._by_stage = {name: tuple(wrappers) for name, wrappers in by_stage.items()}
        self._last = self._counts()

    @property
    def by_stage(self) -> Mapping[str, tuple[ErrorCounter, ...]]:
        return self._by_stage

    def _counts(self) -> dict[int, int]:
        return {
            id(wrapper): int(wrapper.errors)
            for wrappers in self._by_stage.values()
            for wrapper in wrappers
        }

    def take(self) -> frozenset[str]:
        """Stage names whose wrappers counted a new error since the last call."""
        current = self._counts()
        degraded = frozenset(
            name
            for name, wrappers in self._by_stage.items()
            if any(current[id(w)] > self._last.get(id(w), current[id(w)]) for w in wrappers)
        )
        self._last = current
        return degraded

    def all_stages(self) -> frozenset[str]:
        return frozenset(self._by_stage)


def option_chain_from_event_id(print_: OptionsPrint) -> str | None:
    """The UW option chain embedded in the event id, if it matches the print.

    The chain must encode the print's own expiry, option type and strike.
    Anything else (another source's id, a mismatch) returns ``None``.
    """
    for match in _OCC_IN_TEXT.finditer(print_.event_id):
        root, yymmdd, kind, strike_digits = match.groups()
        try:
            expiry = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=UTC).date()
            strike = Decimal(strike_digits) / _STRIKE_SCALE
        except (ValueError, InvalidOperation):
            continue
        wanted_kind = "C" if print_.option_type == "call" else "P"
        if expiry == print_.expiry and kind == wanted_kind and strike == print_.strike:
            return f"{root}{yymmdd}{kind}{strike_digits}"
    return None


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    first_line = text.splitlines()[0][:200] if text else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


class AlfaTelemetryWriter:
    """``DecisionRecordWriter`` that fills ``alfa_stage_telemetry`` and ``alfa_print_meta``.

    - Replay-idempotent: rows whose key already exists are skipped, never
      rewritten.
    - ``close()`` is idempotent. ``Pipeline.run`` closes the writer in its
      ``finally`` block; the worker builds one writer per restart iteration.
    - ``write()`` and ``close()`` never raise.
    - The engine and tables are created lazily on the first write. A failed
      attempt is retried on the next write.
    """

    def __init__(
        self,
        *,
        database_url: str,
        run_id_source: Callable[[], str | None],
        degrading_by_stage: Mapping[str, Sequence[ErrorCounter]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_url = database_url
        self._run_id_source = run_id_source
        self._tracker = _DegradeTracker(degrading_by_stage or {})
        self._clock = clock or (lambda: datetime.now(UTC))
        self._engine: Engine | None = None
        self._sessions: sessionmaker[Session] | None = None
        self._closed = False

    @classmethod
    def for_stages(
        cls,
        *,
        database_url: str,
        run_id_source: Callable[[], str | None],
        stages: Iterable[object],
        clock: Callable[[], datetime] | None = None,
    ) -> AlfaTelemetryWriter:
        """A writer bound to the degrading wrappers found on ``stages``."""
        return cls(
            database_url=database_url,
            run_id_source=run_id_source,
            degrading_by_stage=degrading_wrappers_by_stage(stages),
            clock=clock,
        )

    @property
    def degrading_by_stage(self) -> Mapping[str, tuple[ErrorCounter, ...]]:
        """The wrappers this writer watches, by stage name."""
        return self._tracker.by_stage

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, record: SignalDecisionRecord) -> None:
        """Persist the board's slice of ``record``. Never raises."""
        event_id = "<unknown>"
        try:
            event_id = str(record.event.print_.event_id)
            degraded = self._tracker.take()
        except Exception as exc:
            _logger.warning(
                "alfa telemetry: could not read record %s (%s); marking wrapped stages degraded",
                event_id, _describe(exc),
            )
            degraded = self._tracker.all_stages()
        if self._closed:
            _logger.warning("alfa telemetry writer is closed; event %s not written", event_id)
            return
        try:
            self._persist(record, degraded)
        except Exception as exc:
            _logger.warning(
                "alfa telemetry write failed for event %s; live ingestion unaffected (%s)",
                event_id, _describe(exc),
            )

    def close(self) -> None:
        """Release the engine. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        engine, self._engine, self._sessions = self._engine, None, None
        if engine is None:
            return
        try:
            engine.dispose()
        except Exception as exc:
            _logger.warning("alfa telemetry engine dispose failed (%s)", _describe(exc))

    def _ready(self) -> sessionmaker[Session]:
        if self._sessions is None:
            engine = make_engine(self._database_url)
            try:
                ensure_telemetry_tables(engine)
            except Exception:
                engine.dispose()
                raise
            self._engine = engine
            self._sessions = session_factory(engine)
        return self._sessions

    def _persist(self, record: SignalDecisionRecord, degraded: frozenset[str]) -> None:
        print_ = record.event.print_
        run_id = self._run_id_source()
        if not run_id:
            _logger.warning(
                "alfa telemetry: no active run; event %s not written", print_.event_id,
            )
            return
        sessions = self._ready()
        now = self._clock()
        with sessions() as session:
            has_meta = session.get(AlfaPrintMeta, (run_id, print_.event_id)) is not None
            written = set(
                session.execute(
                    select(AlfaStageTelemetry.stage_name).where(
                        AlfaStageTelemetry.run_id == run_id,
                        AlfaStageTelemetry.event_id == print_.event_id,
                    ),
                ).scalars(),
            )
            if not has_meta:
                session.add(
                    AlfaPrintMeta(
                        run_id=run_id,
                        event_id=print_.event_id,
                        ticker=print_.ticker,
                        option_chain=option_chain_from_event_id(print_),
                        fill_side=print_.fill_side,
                        option_type=print_.option_type,
                        strike=str(print_.strike),
                        expiry=print_.expiry,
                        print_ts=print_.timestamp,
                        written_at=now,
                    ),
                )
            for entry in record.stage_executions:
                if entry.stage_name in written:
                    continue
                written.add(entry.stage_name)
                metadata = entry.metadata
                session.add(
                    AlfaStageTelemetry(
                        run_id=run_id,
                        event_id=print_.event_id,
                        stage_name=entry.stage_name,
                        branch=metadata.get("branch") if metadata else None,
                        metadata_json=(
                            json.dumps(metadata, sort_keys=True, ensure_ascii=False)
                            if metadata is not None
                            else None
                        ),
                        degraded=entry.stage_name in degraded,
                        profile_content_hash=record.profile_content_hash,
                        written_at=now,
                    ),
                )
            session.commit()
