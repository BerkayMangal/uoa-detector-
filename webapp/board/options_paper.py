"""Options PAPER tracker: the daily job that follows open PAPER cards (Phase 5.24).

Scope: ``docs/INDEX.md`` §7 row 5.24 ("exit monitoring and outcome recording").
Log-only paper records, never executed: nothing here sends, routes or places an
order (CLAUDE.md real-money rule).

**What it does, once per trading day after the close** (``outcomes.job_time_et``):

1. **Registers** every committed PAPER card under
   ``artifacts/options-alpha-v1/replay/*/paper_card_*.json`` it has not seen,
   into ``alfa_opt_paper_position``. The card is stored verbatim with its
   ``data_origin`` label, so a replay card is never shown as a live signal.
2. **Marks** each open position: one ``/historic`` call per leg, each call
   behind an atomic quota reservation (``webapp/board/quota_ledger.py``). The
   session calendar is SPY's stored sessions (``alfa_daily_close``), exactly as
   the outcome job uses it, so a holiday shifts the hold instead of becoming an
   unpriced day. Complete marks are appended to ``alfa_opt_paper_mark``.
3. **Decides** with the fixed exit engine (``uoa_detector.options_alpha.tracking``).
   While the card's own plan is still open nothing final is written. A resolved
   plan is written ONCE to ``alfa_opt_paper_outcome`` with all three variants.
   An unknown last horizon day is finalised as ``no_exit_data`` only after one
   further session has closed, so a late vendor row still has a chance.
4. **Heartbeat**: every run appends one ``alfa_opt_paper_heartbeat`` row with
   what it saw and spent, success or not, so "the job is alive" is a stored fact
   and not an assumption.

**What it refuses to do:** invent a price (a missing leg is a missing mark),
borrow an earlier day as an exit, spend a request the ledger did not grant, or
rewrite a final outcome.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, insert, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.options_alpha.exits import ExitReason, ExitVariantId
from uoa_detector.options_alpha.settings import load_settings
from uoa_detector.options_alpha.tracking import (
    CARD_PLAN_VARIANT,
    LegQuote,
    TrackedPosition,
    parse_historic_quotes,
    position_from_card,
    sessions_after,
    track,
)
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.daily_close import BENCHMARK_TICKER, is_final_close, load_closes_by_ticker
from webapp.board.db import AlfaBase
from webapp.board.quota_ledger import ensure_quota_tables, reserve

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine

    from webapp.board.settings import BoardSettings
    from webapp.board.uw_errors import JsonClient

_logger = logging.getLogger(__name__)

OPTIONS_PAPER_JOB_NAME: Final = "options_paper"
OPTION_HISTORIC_PATH: Final = "/api/option-contract/{symbol}/historic"
CARDS_ROOT: Final = Path(__file__).resolve().parents[2] / "artifacts" / "options-alpha-v1" / "replay"
_ET: Final = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Tables (append-only except the rebuildable heartbeat)
# ---------------------------------------------------------------------------


class AlfaOptPaperPosition(AlfaBase):
    """One frozen PAPER card, as registered. Written once, never updated."""

    __tablename__ = "alfa_opt_paper_position"

    position_id: Mapped[str] = mapped_column(String, primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_origin: Mapped[str] = mapped_column(String)
    hypothesis_id: Mapped[str] = mapped_column(String)
    research_status: Mapped[str] = mapped_column(String)
    underlying: Mapped[str] = mapped_column(String, index=True)
    structure: Mapped[str] = mapped_column(String)
    entry_session: Mapped[date] = mapped_column(Date)
    source_path: Mapped[str] = mapped_column(String)
    card_json: Mapped[str] = mapped_column(Text)


class AlfaOptPaperMark(AlfaBase):
    """One complete session mark of one position. Written once per (position, session)."""

    __tablename__ = "alfa_opt_paper_mark"

    position_id: Mapped[str] = mapped_column(String, primary_key=True)
    session: Mapped[date] = mapped_column(Date, primary_key=True)
    closable_value: Mapped[str] = mapped_column(String)
    quotes_json: Mapped[str] = mapped_column(Text)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlfaOptPaperOutcome(AlfaBase):
    """The card plan's final state. Written once, never updated."""

    __tablename__ = "alfa_opt_paper_outcome"

    position_id: Mapped[str] = mapped_column(String, primary_key=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_variant: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    exit_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_value: Mapped[str | None] = mapped_column(String, nullable=True)
    pnl_usd: Mapped[str | None] = mapped_column(String, nullable=True)
    realised: Mapped[bool] = mapped_column(Boolean)
    variants_json: Mapped[str] = mapped_column(Text)


class AlfaOptPaperHeartbeat(AlfaBase):
    """One row per tracker run: proof of life and of what it spent."""

    __tablename__ = "alfa_opt_paper_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    through_session: Mapped[date | None] = mapped_column(Date, nullable=True)
    registered: Mapped[int] = mapped_column(Integer)
    open_positions: Mapped[int] = mapped_column(Integer)
    marks_written: Mapped[int] = mapped_column(Integer)
    outcomes_written: Mapped[int] = mapped_column(Integer)
    requests_granted: Mapped[int] = mapped_column(Integer)
    requests_refused: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text)


_TABLES: Final = (AlfaOptPaperPosition, AlfaOptPaperMark, AlfaOptPaperOutcome, AlfaOptPaperHeartbeat)


def ensure_options_paper_tables(engine: Engine) -> None:
    """Create the tracker's tables and the quota ledger if missing. Never alters or drops."""
    for model in _TABLES:
        cast("Table", model.__table__).create(engine, checkfirst=True)
    ensure_quota_tables(engine)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def card_files(root: Path = CARDS_ROOT) -> list[Path]:
    """Committed PAPER cards, oldest session first. Outcome files are not cards."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/paper_card_*.json") if not p.stem.endswith("_outcome"))


def register_cards(engine: Engine, *, root: Path = CARDS_ROOT, now: datetime) -> int:
    """Insert every card not yet registered. Returns how many were new."""
    new = 0
    with engine.begin() as conn:
        known = set(conn.execute(select(AlfaOptPaperPosition.position_id)).scalars())
        for path in card_files(root):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))["card"]
            except (OSError, ValueError, KeyError):
                _logger.warning("options paper: unreadable card %s", path)
                continue
            position_id = str(card.get("signal_id") or "")
            if not position_id or position_id in known:
                continue
            conn.execute(insert(AlfaOptPaperPosition).values(
                position_id=position_id,
                registered_at=now,
                data_origin=str(card.get("data_origin") or "unknown"),
                hypothesis_id=str(card.get("hypothesis_id") or ""),
                research_status=str(card.get("research_status") or ""),
                underlying=str(card["underlying"]),
                structure=str(card["structure"]),
                entry_session=date.fromisoformat(str(card["session"])),
                source_path=str(path.relative_to(root)),
                card_json=json.dumps(card, ensure_ascii=False, sort_keys=True),
            ))
            known.add(position_id)
            new += 1
    return new


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class TrackerReport:
    through_session: date | None = None
    registered: int = 0
    open_positions: int = 0
    marks_written: int = 0
    outcomes_written: int = 0
    requests_granted: int = 0
    requests_refused: int = 0
    degraded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.requests_refused:
            return "quota_refused"
        if self.degraded:
            return "degraded"
        return "ok"

    def detail(self) -> str:
        parts = [
            f"registered {self.registered}", f"open {self.open_positions}",
            f"marks {self.marks_written}", f"outcomes {self.outcomes_written}",
            f"requests {self.requests_granted} granted / {self.requests_refused} refused",
        ]
        if self.through_session is not None:
            parts.append(f"through {self.through_session.isoformat()}")
        if self.degraded:
            parts.append("degraded " + ",".join(self.degraded))
        parts.extend(self.notes)
        return "; ".join(parts)


class QuotaRefusedError(Exception):
    """The ledger refused a reservation: stop spending for today."""


def _observed(client: JsonClient) -> int | None:
    value = getattr(client, "last_daily_request_count", None)
    return value if isinstance(value, int) else None


async def _historic(
    client: JsonClient, engine: Engine, symbol: str, *, day: date, cap: int, report: TrackerReport,
) -> dict[date, LegQuote] | None:
    """One reserved ``/historic`` fetch. ``None`` when OUR fetch failed (degraded)."""
    grant = reserve(engine, day=day, n=1, cap=cap, observed=_observed(client))
    if not grant.granted:
        report.requests_refused += 1
        raise QuotaRefusedError(f"ledger at {grant.reserved_after}/{grant.cap}")
    report.requests_granted += 1
    try:
        payload = await client.request_json(OPTION_HISTORIC_PATH.format(symbol=symbol))
    except UnusualWhalesNotFoundError:
        return {}
    except UnusualWhalesDailyLimitError:
        raise
    except (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError) as exc:
        _logger.warning("options paper: historic degraded for %s: %s", symbol, exc)
        report.degraded.append(symbol)
        return None
    return parse_historic_quotes(payload)


def _open_positions(engine: Engine) -> list[tuple[str, TrackedPosition]]:
    with engine.begin() as conn:
        resolved = set(conn.execute(select(AlfaOptPaperOutcome.position_id)).scalars())
        rows = conn.execute(select(AlfaOptPaperPosition.position_id, AlfaOptPaperPosition.card_json)).all()
    out = []
    for position_id, card_json in rows:
        if position_id in resolved:
            continue
        out.append((position_id, position_from_card(json.loads(card_json))))
    return sorted(out, key=lambda item: (item[1].entry_session, item[0]))


def _store_marks(engine: Engine, position_id: str, marks: Sequence[Any], now: datetime) -> int:
    written = 0
    with engine.begin() as conn:
        have = set(conn.execute(
            select(AlfaOptPaperMark.session).where(AlfaOptPaperMark.position_id == position_id)
        ).scalars())
        for mark in marks:
            if not mark.is_complete or mark.session in have:
                continue
            conn.execute(insert(AlfaOptPaperMark).values(
                position_id=position_id,
                session=mark.session,
                closable_value=str(mark.closable_value),
                quotes_json=json.dumps({
                    occ: {"bid": None if q.bid is None else str(q.bid),
                          "ask": None if q.ask is None else str(q.ask),
                          "volume": q.volume}
                    for occ, q in mark.quotes.items()
                }, sort_keys=True),
                marked_at=now,
            ))
            written += 1
    return written


def _variants_json(outcomes: Mapping[ExitVariantId, Any]) -> str:
    return json.dumps({
        variant.value: {
            "reason": o.reason.value,
            "exit_day": None if o.exit_day is None else o.exit_day.isoformat(),
            "exit_value": None if o.exit_value is None else str(o.exit_value),
            "pnl_usd": None if o.pnl_usd is None else str(o.pnl_usd),
            "held": o.held_trading_days,
            "unpriced_days": o.unpriced_days,
            "notes": list(o.notes),
        }
        for variant, o in outcomes.items()
    }, ensure_ascii=False, sort_keys=True)


async def run_options_paper(
    *,
    client: JsonClient,
    engine: Engine,
    settings: BoardSettings,
    now: datetime,
    root: Path = CARDS_ROOT,
) -> TrackerReport:
    """Register, mark, decide, heartbeat. Daily-limit and auth errors propagate."""
    ensure_options_paper_tables(engine)
    report = TrackerReport()
    cap = int(settings.refresh.daily_request_soft_cap)
    et_day = now.astimezone(_ET).date()
    status_override: str | None = None
    try:
        report.registered = register_cards(engine, root=root, now=now)
        spy = load_closes_by_ticker(engine, [BENCHMARK_TICKER]).get(BENCHMARK_TICKER, ())
        calendar = [p.day for p in spy if is_final_close(p.day, now)]
        if not calendar:
            report.notes.append("no stored SPY sessions yet; nothing marked")
        through = max(calendar) if calendar else None
        report.through_session = through
        positions = _open_positions(engine)
        report.open_positions = len(positions)
        options_settings = load_settings()
        for position_id, position in positions if through else []:
            assert through is not None
            if position.entry_session < min(calendar):
                report.notes.append(f"{position_id}: entry before stored calendar")
                continue
            sessions = sessions_after(calendar, position.entry_session, through)
            if not sessions:
                continue
            quotes: dict[str, dict[date, LegQuote]] = {}
            failed = False
            for leg in position.legs:
                fetched = await _historic(
                    client, engine, leg.occ_symbol, day=et_day, cap=cap, report=report,
                )
                if fetched is None:
                    failed = True
                    break
                quotes[leg.occ_symbol] = fetched
            if failed:
                continue  # our failure: no mark, no outcome, try again next run
            result = track(position, sessions, quotes, options_settings)
            report.marks_written += _store_marks(engine, position_id, result.marks, now)
            plan = result.plan
            if not result.resolved:
                continue
            if plan.reason is ExitReason.NO_EXIT_DATA:
                # The unknown horizon day must be at least one session old before it
                # is frozen into an append-only row: a late vendor row still counts.
                horizon_last = sessions[plan.held_trading_days - 1]
                if horizon_last == through:
                    report.notes.append(f"{position_id}: horizon day unpriced, waiting one session")
                    continue
            with engine.begin() as conn:
                exists = conn.execute(select(AlfaOptPaperOutcome.position_id).where(
                    AlfaOptPaperOutcome.position_id == position_id)).first()
                if exists is None:
                    conn.execute(insert(AlfaOptPaperOutcome).values(
                        position_id=position_id,
                        resolved_at=now,
                        plan_variant=CARD_PLAN_VARIANT.value,
                        reason=plan.reason.value,
                        exit_day=plan.exit_day,
                        exit_value=None if plan.exit_value is None else str(plan.exit_value),
                        pnl_usd=None if plan.pnl_usd is None else str(plan.pnl_usd),
                        realised=plan.pnl_usd is not None,
                        variants_json=_variants_json(result.outcomes),
                    ))
                    report.outcomes_written += 1
    except QuotaRefusedError as exc:
        report.notes.append(f"quota refused: {exc}")
    except Exception as exc:
        status_override = "failed"
        report.notes.append(f"error: {type(exc).__name__}")
        _write_heartbeat(engine, report, now, status_override)
        raise
    _write_heartbeat(engine, report, now, status_override)
    return report


def _write_heartbeat(engine: Engine, report: TrackerReport, now: datetime, status: str | None) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(insert(AlfaOptPaperHeartbeat).values(
                run_at=now,
                through_session=report.through_session,
                registered=report.registered,
                open_positions=report.open_positions,
                marks_written=report.marks_written,
                outcomes_written=report.outcomes_written,
                requests_granted=report.requests_granted,
                requests_refused=report.requests_refused,
                status=status or report.status,
                detail=report.detail()[:2000],
            ))
    except Exception:
        _logger.exception("options paper: heartbeat write failed")


# ---------------------------------------------------------------------------
# Read side for /opsiyon
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedRow:
    position_id: str
    underlying: str
    structure: str
    data_origin: str
    hypothesis_id: str
    entry_session: date
    last_mark_session: date | None
    last_mark_value: str | None
    marks: int
    reason: str | None
    exit_day: date | None
    pnl_usd: str | None
    realised: bool | None

    @property
    def state(self) -> str:
        if self.reason is None:
            return "acik"
        return "sonuclandi" if self.realised else "bilinmiyor"


@dataclass(frozen=True)
class TrackerView:
    rows: tuple[TrackedRow, ...]
    last_run_at: datetime | None
    last_status: str | None
    last_detail: str | None
    runs: int


def read_tracker(engine: Engine) -> TrackerView:
    """Everything /opsiyon shows about the tracker. Read-only; empty when never run.

    Creates nothing: a web request must not write DDL. Missing tables mean the job
    has never run, which the screen says in words.
    """
    tables = set(inspect(engine).get_table_names())
    if not {model.__tablename__ for model in _TABLES} <= tables:
        return TrackerView(rows=(), last_run_at=None, last_status=None, last_detail=None, runs=0)
    with Session(engine) as session:
        positions = session.scalars(select(AlfaOptPaperPosition)).all()
        outcomes = {o.position_id: o for o in session.scalars(select(AlfaOptPaperOutcome))}
        marks: dict[str, list[AlfaOptPaperMark]] = {}
        for mark in session.scalars(select(AlfaOptPaperMark)):
            marks.setdefault(mark.position_id, []).append(mark)
        beat = session.scalars(
            select(AlfaOptPaperHeartbeat).order_by(AlfaOptPaperHeartbeat.run_at.desc()).limit(1)
        ).first()
        runs = len(session.scalars(select(AlfaOptPaperHeartbeat.id)).all())
    rows = []
    for p in positions:
        own = sorted(marks.get(p.position_id, []), key=lambda m: m.session)
        o = outcomes.get(p.position_id)
        rows.append(TrackedRow(
            position_id=p.position_id, underlying=p.underlying, structure=p.structure,
            data_origin=p.data_origin, hypothesis_id=p.hypothesis_id,
            entry_session=p.entry_session,
            last_mark_session=own[-1].session if own else None,
            last_mark_value=own[-1].closable_value if own else None,
            marks=len(own),
            reason=None if o is None else o.reason,
            exit_day=None if o is None else o.exit_day,
            pnl_usd=None if o is None else o.pnl_usd,
            realised=None if o is None else o.realised,
        ))
    rows.sort(key=lambda r: (r.entry_session, r.position_id), reverse=True)
    return TrackerView(
        rows=tuple(rows),
        last_run_at=None if beat is None else _aware(beat.run_at),
        last_status=None if beat is None else beat.status,
        last_detail=None if beat is None else beat.detail,
        runs=runs,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Registry entry
# ---------------------------------------------------------------------------


class OptionsPaperContext(Protocol):
    """What the job needs from the daily-job ``JobContext`` (checked in daily_jobs)."""

    @property
    def client(self) -> JsonClient: ...
    @property
    def engine(self) -> Engine: ...
    @property
    def settings(self) -> BoardSettings: ...
    @property
    def now(self) -> datetime: ...


async def run_options_paper_job(ctx: OptionsPaperContext) -> str:
    """The daily-job body: ``JobContext`` in, a short detail out."""
    report = await run_options_paper(
        client=ctx.client, engine=ctx.engine, settings=ctx.settings, now=ctx.now,
    )
    return report.detail()
