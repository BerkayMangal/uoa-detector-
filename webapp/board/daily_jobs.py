"""Daily-job clock for the Alfa Board refresher (Phase 5.2.B-jobs).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Daily jobs run in
the same refresher on profile clock times (ET)"), §4.4 (the daily rows of the
request budget) and §7 (scheduling of the FAZ D data jobs).

Why this exists: the refresher loop is gated on regular trading hours and
sleeps outside them, so a pre-market job at 07:15 ET and a post-close job at
17:00 ET would never run. This module is the clock the loop checks on **every**
tick, RTH or not.

**Registry.** ONE ordered list, built from the board profile:

1. ``oi_confirm`` — pre-market at ``opening_closing.job_time_et``: the previous
   ET session's dominant contracts are flagged, then pending rows are resolved
   against the T+1 open interest (B4).
2. ``catalysts`` — same time: earnings and FDA per ticker plus the economic
   calendar (2N + 1 requests).
3. ``gamma_history`` — after the open: SPY and QQQ one-year gamma percentile.
4. ``etf_holdings`` — post-close at ``delayed.job_time_et``: the focused list.
5. ``daily_close`` — same time: ``ohlc/1d`` per ticker plus SPY.
6. ``delayed`` — same time: congress, insider, short interest and FTDs.
7. ``outcomes`` — post-close at ``outcomes.job_time_et``: FAZ C scores every
   decision card against its horizons (``webapp/board/outcome_job.py``).
8. ``options_paper`` — same time, registered just before ``outcomes`` so that
   FAZ C's entry stays last: the options PAPER tracker marks every open PAPER
   card and records its outcome (``webapp/board/options_paper.py``).

Entry 7 is appended by ``build_registry`` itself, not by its caller. The
refresher runs exactly the tuple this function returns, so a job left out of it
never runs at all — which is what happened to ``outcomes`` until 2026-09-17.

**Due rule**, checked on every tick. A job is due when all of these hold:

- ``now`` in ET is at or past the job's time on today's ET date;
- today is a trading day (``trading_day_only`` jobs only);
- ``alfa_job_run`` holds no success marker for (job, ET date);
- its last attempt, if any, is at least ``refresh.cadence_seconds`` old, so a
  failed job retries at most once per cadence;
- it has been attempted fewer than ``refresh.daily_job_max_attempts`` times
  today, so a job that keeps failing stops instead of re-spending its requests
  every cadence until ET midnight (review FB-H1/FB-02).

**Markers.** ``alfa_job_run`` (job_name, et_date) records status, start, finish,
the attempt count and a short detail. It is rebuildable: it carries no forward evidence, only
what ran. Because the marker is keyed by the ET date and written on success, a
Railway restart neither repeats a finished day's job nor skips one that had not
run yet — catch-up after a restart is intended.

**Holidays are not modelled**, exactly as ``uoa_detector.sources.market_hours``
documents for the RTH gate: a market holiday is a weekday here, and the cost is
one wasted set of requests whose jobs then store nothing.

**Isolation.** One job's exception is logged and marked ``failed``; the rest of
the due jobs still run. ``UnusualWhalesDailyLimitError`` and a key failure
(HTTP 401/403) propagate to the loop, which backs off. Any other 4xx and the
degraded-error family are that job's own business: the data layers already
report them without raising. A not-found answer is no data (program rule 9): it
is marked ``success`` with a ``no data`` detail, so an empty source does not
become a daily retry loop.

**Soft cap.** Daily jobs are non-critical fetches (contract §4.1), so the loop
hands its ``SoftCap`` in as ``cap``. While it is reached, the step runs nothing
and marks nothing: every pending job stays due for a later tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
)
from webapp.board.aggregate import build_board_rows
from webapp.board.catalysts import refresh_catalysts
from webapp.board.daily_close import run_daily_close_job
from webapp.board.db import AlfaBase, session_factory
from webapp.board.delayed import run_delayed_job
from webapp.board.etf_holdings import refresh_etf_holdings
from webapp.board.oi_confirm import FlaggedContract, confirm_open_interest
from webapp.board.options_paper import OPTIONS_PAPER_JOB_NAME, run_options_paper_job
from webapp.board.outcome_job import outcome_jobs
from webapp.board.quotes import dominant_symbol
from webapp.board.regime import refresh_gamma_history
from webapp.board.signals import live_run_tips
from webapp.board.uw_errors import JsonClient, is_key_failure

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.aggregate import BoardRow
    from webapp.board.outcome_job import OutcomeJob
    from webapp.board.settings import BoardSettings
    from webapp.board.signals import BoardPrint
    from webapp.journal import TradeRow

_logger = logging.getLogger(__name__)

JobStatus = Literal["running", "success", "failed"]

_ET: Final = ZoneInfo("America/New_York")
# Market structure, not a cutoff: the US regular session opens at 09:30 ET.
SESSION_OPEN_ET: Final = time(9, 30)
_SATURDAY: Final = 5
_MAX_LOOKAHEAD_DAYS: Final = 8  # a weekend plus a margin; the clock never scans further
# Market structure, not a cutoff: one US equity option contract covers 100 shares.
_CONTRACT_MULTIPLIER: Final = Decimal(100)
_OPEN_STATUS: Final = "open"
_DETAIL_LIMIT: Final = 500  # the marker stores a short line, never a payload


# ---------------------------------------------------------------------------
# Marker table
# ---------------------------------------------------------------------------


class AlfaJobRun(AlfaBase):
    """One daily job's run for one ET date (rebuildable: it records what ran, not evidence)."""

    __tablename__ = "alfa_job_run"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    et_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    # Phase 5.2.B-fix1: attempts of this (job, ET date), so a job that keeps failing stops.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def ensure_job_run_tables(engine: Engine) -> None:
    """Create ``alfa_job_run`` if missing. Never alters or drops anything."""
    cast("Table", AlfaJobRun.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class JobState:
    """The stored marker of one job on one ET date."""

    status: JobStatus
    started_at: datetime
    finished_at: datetime | None
    detail: str | None
    attempts: int = 0  # Phase 5.2.B-fix1: attempts started on this ET date

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class RunReader(Protocol):
    """What the jobs need from ``BoardSignalReader``."""

    def load_run(self, run_id: str) -> list[BoardPrint]: ...


class OpenTrades(Protocol):
    """What the jobs need from ``JournalRepo``."""

    def list(self, status: str | None = ...) -> list[TradeRow]: ...


@dataclass(frozen=True)
class JobContext:
    """Everything a daily job may use. Built once per tick, for the due jobs only."""

    client: JsonClient
    engine: Engine
    sessions: sessionmaker[Session]
    settings: BoardSettings
    now: datetime
    tickers: tuple[str, ...]  # board tickers of the live run plus open journal tickers
    reader: RunReader


class JobRun(Protocol):
    """One job body: it runs and returns a short English detail for the marker."""

    async def __call__(self, ctx: JobContext) -> str: ...


class RequestCap(Protocol):
    """What the clock needs from the refresher's ``SoftCap`` (contract §4.1).

    The clock only asks whether the key-wide daily request count has reached
    the cap; the refresher's adapter observes the client's latest count first,
    so the answer stays current between two jobs of the same tick.
    """

    def reached(self, now: datetime) -> bool: ...


@dataclass(frozen=True)
class DailyJob:
    name: str
    et_time: time
    run: JobRun
    trading_day_only: bool = True


@dataclass(frozen=True)
class JobsReport:
    et_date: date
    due: tuple[str, ...]
    ran: tuple[tuple[str, JobStatus, str], ...]  # (job, status, detail)

    @property
    def requests_made(self) -> bool:
        return bool(self.ran)


async def run_oi_confirm(ctx: JobContext) -> str:
    """B4: flag the previous ET session's dominant contracts, then resolve pending rows."""
    flagged = await asyncio.to_thread(
        previous_session_flags, ctx.engine, ctx.reader, settings=ctx.settings, now=ctx.now,
    )
    report = await confirm_open_interest(
        ctx.client, ctx.sessions, flagged=flagged, settings=ctx.settings, now=ctx.now,
    )
    return (
        f"flagged={report.recorded} already={report.already_recorded} "
        f"resolved={len(report.resolved)} awaiting={len(report.awaiting)} "
        f"requests={report.requests}"
    )


async def run_catalysts(ctx: JobContext) -> str:
    """B4: earnings and FDA per ticker, plus the market-wide economic calendar."""
    report = await refresh_catalysts(
        ctx.client, ctx.sessions, tickers=ctx.tickers, settings=ctx.settings, now=ctx.now,
    )
    stored = sum(rows for _source, _ticker, rows in report.stored)
    return (
        f"events={stored} requests={report.requests} "
        f"no_data={len(report.no_data)} degraded={len(report.degraded)}"
    )


async def run_gamma_history(ctx: JobContext) -> str:
    """B5: the SPY and QQQ one-year gamma percentile and negative-day base rate."""
    report = await refresh_gamma_history(
        ctx.client, ctx.sessions, settings=ctx.settings, now=ctx.now,
    )
    return (
        f"stored={len(report.stored)} requests={report.requests} "
        f"no_data={len(report.no_data)} degraded={len(report.degraded)}"
    )


async def run_etf_holdings(ctx: JobContext) -> str:
    """B6: rebuild the focused ETFs' holdings."""
    report = await refresh_etf_holdings(
        ctx.client, ctx.sessions, settings=ctx.settings, now=ctx.now,
    )
    return (
        f"etfs={len(report.stored)} broad={len(report.skipped_broad)} "
        f"requests={report.requests} degraded={len(report.degraded)}"
    )


async def run_daily_closes(ctx: JobContext) -> str:
    """FAZ D: the regular-session closes behind every delayed row's outcome."""
    result = await run_daily_close_job(ctx.client, ctx.engine, ctx.tickers, now=ctx.now)
    inserted = sum(r.inserted for r in result.tickers)
    return (
        f"tickers={len(result.tickers)} closes={inserted} degraded={len(result.degraded)}"
    )


async def run_delayed_families(ctx: JobContext) -> str:
    """FAZ D: congress, insider, short interest and FTDs (four requests per ticker)."""
    result = await run_delayed_job(
        ctx.client, ctx.engine, ctx.tickers, now=ctx.now, settings=ctx.settings.delayed,
    )
    inserted = sum(r.inserted for r in result.results)
    return (
        f"families={len(result.results)} rows={inserted} degraded={len(result.degraded)}"
    )


def _as_daily_job(job: OutcomeJob) -> DailyJob:
    """FAZ C's own entry shape as a registry entry.

    ``outcome_job.py`` types its body against a Protocol instead of importing
    ``JobContext`` from here, which would be a cycle. This adapter is where
    ``JobContext`` is checked against that Protocol under ``mypy --strict``.
    """

    async def run(ctx: JobContext) -> str:
        return await job.run(ctx)

    return DailyJob(
        name=job.name, et_time=job.et_time, run=run, trading_day_only=job.trading_day_only,
    )


def build_registry(settings: BoardSettings) -> tuple[DailyJob, ...]:
    """The ordered daily-job list, FAZ C's outcome job included as the last entry."""
    pre_market = time.fromisoformat(settings.opening_closing.job_time_et)
    post_close = time.fromisoformat(settings.delayed.job_time_et)
    return (
        DailyJob(name="oi_confirm", et_time=pre_market, run=run_oi_confirm),
        DailyJob(name="catalysts", et_time=pre_market, run=run_catalysts),
        DailyJob(name="gamma_history", et_time=SESSION_OPEN_ET, run=run_gamma_history),
        DailyJob(name="etf_holdings", et_time=post_close, run=run_etf_holdings),
        DailyJob(name="daily_close", et_time=post_close, run=run_daily_closes),
        DailyJob(name="delayed", et_time=post_close, run=run_delayed_families),
        # FAZ C's entry, built by the module that owns it so the time and the body
        # have one source. Appended here because the refresher runs this tuple.
        # Phase 5.24: the options PAPER tracker, on the outcome job's clock time
        # (the stored SPY close it uses as a calendar is final by then). Placed
        # before FAZ C's entry, which stays last.
        DailyJob(
            name=OPTIONS_PAPER_JOB_NAME,
            et_time=time.fromisoformat(settings.outcomes.job_time_et),
            run=_options_paper,
        ),
        *(_as_daily_job(j) for j in outcome_jobs(settings)),
    )


async def _options_paper(ctx: JobContext) -> str:
    """Where ``JobContext`` is checked against the tracker's Protocol under mypy --strict."""
    return await run_options_paper_job(ctx)


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def is_trading_day(day: date) -> bool:
    """A weekday. Market holidays are not modelled (see the module docstring)."""
    return day.weekday() < _SATURDAY


def et_date(moment: datetime) -> date:
    return _as_utc(moment).astimezone(_ET).date()


def next_trading_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def previous_session(day: date) -> date:
    """The previous weekday: the session whose prints a pre-market job confirms."""
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev


def scheduled_at(job: DailyJob, day: date) -> datetime:
    """The instant ``job`` is due on ``day``, as an aware ET datetime."""
    return datetime.combine(day, job.et_time, tzinfo=_ET)


def is_due(
    job: DailyJob,
    *,
    now: datetime,
    state: JobState | None,
    cadence_seconds: int,
    max_attempts: int | None = None,
) -> bool:
    """Contract §4.1: past its ET time today, a trading day, not done, not retried too soon.

    ``max_attempts`` bounds the attempts of one (job, ET date): once they are
    spent the job is no longer offered today (None: no bound, the behaviour
    before Phase 5.2.B-fix1).
    """
    today = et_date(now)
    if job.trading_day_only and not is_trading_day(today):
        return False
    if _as_utc(now) < scheduled_at(job, today).astimezone(UTC):
        return False
    if state is None:
        return True
    if state.succeeded:
        return False
    if _as_utc(now) - _as_utc(state.started_at) < timedelta(seconds=cadence_seconds):
        return False
    return max_attempts is None or state.attempts < max_attempts


def due_jobs(
    jobs: Sequence[DailyJob],
    *,
    now: datetime,
    states: Mapping[str, JobState],
    cadence_seconds: int,
    max_attempts: int | None = None,
) -> list[DailyJob]:
    """The due jobs, in registry order."""
    return [
        job for job in jobs
        if is_due(
            job, now=now, state=states.get(job.name), cadence_seconds=cadence_seconds,
            max_attempts=max_attempts,
        )
    ]


def seconds_until_next_job(
    jobs: Sequence[DailyJob],
    *,
    now: datetime,
    states: Mapping[str, JobState],
    cadence_seconds: int,
    max_attempts: int | None = None,
) -> float | None:
    """Seconds until the next job is due (0.0 when one is due now), or None with no jobs.

    Outside regular trading hours the loop sleeps the shorter of this and
    ``refresh.closed_market_sleep_seconds``, so it wakes for a post-close or
    pre-market job instead of sleeping past it.
    """
    moment = _as_utc(now)
    waits: list[float] = []
    for job in jobs:
        state = states.get(job.name)
        if is_due(
            job, now=now, state=state, cadence_seconds=cadence_seconds,
            max_attempts=max_attempts,
        ):
            return 0.0
        waits.append(_wait_for(
            job, now=moment, state=state, cadence_seconds=cadence_seconds,
            max_attempts=max_attempts,
        ))
    return min(waits) if waits else None


def _wait_for(
    job: DailyJob,
    *,
    now: datetime,
    state: JobState | None,
    cadence_seconds: int,
    max_attempts: int | None = None,
) -> float:
    today = et_date(now)
    day = today
    if state is not None and state.succeeded:
        day = next_trading_day(today) if job.trading_day_only else today + timedelta(days=1)
    candidates: list[datetime] = []
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if not job.trading_day_only or is_trading_day(day):
            due_at = scheduled_at(job, day).astimezone(UTC)
            if state is not None and not state.succeeded and day == today:
                if max_attempts is not None and state.attempts >= max_attempts:
                    # Today's attempts are spent: the next chance is the job's next day.
                    day += timedelta(days=1)
                    continue
                due_at = max(due_at, _as_utc(state.started_at) + timedelta(seconds=cadence_seconds))
            if due_at > now:
                candidates.append(due_at)
                break
        day += timedelta(days=1)
    if not candidates:
        return float(_MAX_LOOKAHEAD_DAYS * 24 * 3600)
    return max(0.0, (candidates[0] - now).total_seconds())


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


# The engine whose marker table is known to exist (checked once, like the read paths).
_tables_ready_for: list[Engine] = []


def _ready(engine: Engine) -> None:
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_job_run_tables(engine)
        _tables_ready_for[:] = [engine]


def read_job_states(engine: Engine, *, day: date) -> dict[str, JobState]:
    """Every marker stored for ``day`` (ET), by job name."""
    _ready(engine)
    with Session(engine) as session:
        rows = session.scalars(select(AlfaJobRun).where(AlfaJobRun.et_date == day)).all()
    return {
        row.job_name: JobState(
            status=_status(row.status),
            started_at=_as_utc(row.started_at),
            finished_at=_as_utc(row.finished_at) if row.finished_at is not None else None,
            detail=row.detail,
            attempts=row.attempts or 0,
        )
        for row in rows
    }


def mark_started(engine: Engine, *, job: str, day: date, now: datetime) -> None:
    """Record the attempt before it runs, so a crash still spaces the retry and counts."""
    _write(
        engine, job=job, day=day, status="running", started_at=_as_utc(now),
        finished_at=None, detail=None, count_attempt=True,
    )


def mark_finished(
    engine: Engine, *, job: str, day: date, now: datetime, status: JobStatus, detail: str,
) -> None:
    _write(
        engine, job=job, day=day, status=status, started_at=None,
        finished_at=_as_utc(now), detail=detail[:_DETAIL_LIMIT],
    )


def _write(
    engine: Engine,
    *,
    job: str,
    day: date,
    status: JobStatus,
    started_at: datetime | None,
    finished_at: datetime | None,
    detail: str | None,
    count_attempt: bool = False,
) -> None:
    _ready(engine)
    with Session(engine) as session, session.begin():
        row = session.get(AlfaJobRun, (job, day))
        if row is None:
            session.add(AlfaJobRun(
                job_name=job, et_date=day, status=status,
                started_at=started_at or finished_at or datetime.now(UTC),
                finished_at=finished_at, detail=detail, attempts=int(count_attempt),
            ))
            return
        if count_attempt:
            row.attempts = (row.attempts or 0) + 1
        row.status = status
        if started_at is not None:
            row.started_at = started_at
        if finished_at is not None:
            row.finished_at = finished_at
        if detail is not None:
            row.detail = detail


def _status(raw: str) -> JobStatus:
    if raw in ("running", "success", "failed"):
        return cast("JobStatus", raw)
    msg = f"unknown alfa_job_run status {raw!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Inputs (database reads)
# ---------------------------------------------------------------------------


def latest_live_run(engine: Engine, *, now: datetime) -> str | None:
    """The live run holding today's prints, or the newest live run otherwise."""
    tips = live_run_tips(engine)
    if not tips:
        return None
    today = et_date(now)
    todays = [(ts, run_id) for ts, run_id in tips if et_date(ts) == today]
    _newest, run_id = max(todays or tips)
    return run_id


def session_live_run(engine: Engine, *, day: date) -> str | None:
    """The live run whose newest print falls on ``day`` (ET), or None."""
    on_day = [(ts, run_id) for ts, run_id in live_run_tips(engine) if et_date(ts) == day]
    if not on_day:
        return None
    _newest, run_id = max(on_day)
    return run_id


def job_tickers(
    engine: Engine,
    reader: RunReader,
    journal: OpenTrades | None,
    *,
    settings: BoardSettings,
    now: datetime,
) -> tuple[str, ...]:
    """Board tickers of today's (or the latest) live run, then the open journal tickers."""
    tickers: list[str] = []
    try:
        run_id = latest_live_run(engine, now=now)
        rows = (
            build_board_rows(reader.load_run(run_id), settings.aggregation)
            if run_id is not None
            else []
        )
    except Exception:
        # A database with no live run yet (a fresh deploy) must not fail every daily
        # job: SPY closes and the macro calendar still have work to do.
        _logger.warning("board daily jobs: live run unreadable; journal tickers only", exc_info=True)
    else:
        tickers.extend(row.ticker.strip().upper() for row in rows)
    if journal is not None:
        try:
            open_trades = journal.list(_OPEN_STATUS)
        except Exception:
            _logger.warning("board daily jobs: open journal read failed; board tickers only", exc_info=True)
        else:
            tickers.extend(_journal_tickers(open_trades))
    return tuple(dict.fromkeys(t for t in tickers if t))


def _journal_tickers(trades: Iterable[TradeRow]) -> list[str]:
    return [
        trade.ticker.strip().upper()
        for trade in trades
        if isinstance(trade.ticker, str) and trade.ticker.strip()
    ]


def previous_session_flags(
    engine: Engine,
    reader: RunReader,
    *,
    settings: BoardSettings,
    now: datetime,
) -> list[FlaggedContract]:
    """One ``FlaggedContract`` per dominant contract of the previous ET session's live run.

    The size is the session's aggregated contract count for that contract
    (premium / (print price x 100), summed over its prints). A contract that
    appears in two rows (bought and sold) keeps the first recorded size, which
    is the row with the larger premium.
    """
    day = previous_session(et_date(now))
    run_id = session_live_run(engine, day=day)
    if run_id is None:
        return []
    rows = build_board_rows(reader.load_run(run_id), settings.aggregation)
    flags: list[FlaggedContract] = []
    seen: set[str] = set()
    for row in rows:
        symbol = dominant_symbol(row)
        if symbol is None or symbol in seen:
            continue
        size = flagged_size(row)
        if size <= 0:
            continue
        seen.add(symbol)
        flags.append(FlaggedContract(
            option_symbol=symbol, ticker=row.ticker.strip().upper(),
            trade_date=day, flagged_size=size,
        ))
    return flags


def flagged_size(row: BoardRow) -> int:
    """Contracts printed on the row's dominant contract: premium / (print price x 100)."""
    key = row.dominant.key
    total = 0
    for print_ in row.prints:
        same = (print_.expiry, print_.strike, print_.option_type) == (
            key.expiry, key.strike, key.option_type
        )
        if not same or print_.option_price <= 0:
            continue
        contracts = print_.premium / (print_.option_price * _CONTRACT_MULTIPLIER)
        total += int(contracts.to_integral_value(rounding=ROUND_HALF_UP))
    return total


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


async def run_daily_jobs(
    client: JsonClient,
    engine: Engine,
    settings: BoardSettings,
    *,
    jobs: Sequence[DailyJob],
    reader: RunReader,
    journal: OpenTrades | None,
    now: datetime,
    cap: RequestCap | None = None,
) -> JobsReport:
    """Run every due job once, in registry order.

    Nothing is read from the database until at least one job is due, so a tick
    with nothing to do costs one small marker query. A job's own exception is
    logged and marked ``failed``; the daily limit and a key failure propagate.

    ``cap`` is the refresher's daily-request soft cap. While it is reached the
    step runs nothing and marks nothing, so every pending job stays due for a
    later tick (contract §4.1; review FB-H1/FB-01).
    """
    day = et_date(now)
    cadence = settings.refresh.cadence_seconds
    states = await asyncio.to_thread(read_job_states, engine, day=day)
    pending = due_jobs(
        jobs, now=now, states=states, cadence_seconds=cadence,
        max_attempts=settings.refresh.daily_job_max_attempts,
    )
    if not pending:
        return JobsReport(et_date=day, due=(), ran=())
    if cap is not None and cap.reached(now):
        _logger.warning(
            "board daily jobs: daily request soft cap reached; %d job(s) wait for a later tick",
            len(pending),
        )
        return JobsReport(et_date=day, due=tuple(job.name for job in pending), ran=())
    tickers = await asyncio.to_thread(
        job_tickers, engine, reader, journal, settings=settings, now=now,
    )
    ctx = JobContext(
        client=client, engine=engine, sessions=session_factory(engine), settings=settings,
        now=now, tickers=tickers, reader=reader,
    )
    ran: list[tuple[str, JobStatus, str]] = []
    for job in pending:
        if cap is not None and cap.reached(now):
            _logger.warning(
                "board daily jobs: daily request soft cap reached; %s waits for a later tick",
                job.name,
            )
            break
        await asyncio.to_thread(mark_started, engine, job=job.name, day=day, now=now)
        try:
            detail = await job.run(ctx)
        except UnusualWhalesDailyLimitError as exc:
            await _finish(engine, job.name, day, now, "failed", f"daily limit: {exc}")
            raise
        except UnusualWhalesNotFoundError as exc:
            # Program rule 9: a not-found answer is no data, not a failure. Marking it
            # failed would re-spend the job's requests every cadence (review FB-02).
            _logger.warning("board daily job %s: no data (%s)", job.name, exc)
            await _finish(engine, job.name, day, now, "success", f"no data: {exc}")
            ran.append((job.name, "success", "no data"))
        except UnusualWhalesAuthError as exc:
            await _finish(engine, job.name, day, now, "failed", f"auth: {exc}")
            if is_key_failure(exc):
                _logger.error("board daily job %s: UW key failure", job.name)
                raise
            _logger.warning("board daily job %s rejected by UW (%s); marked failed", job.name, exc)
            ran.append((job.name, "failed", "rejected"))
        except Exception as exc:
            _logger.exception("board daily job %s failed; retry after one cadence", job.name)
            await _finish(engine, job.name, day, now, "failed", f"error: {exc}")
            ran.append((job.name, "failed", "error"))
        else:
            await _finish(engine, job.name, day, now, "success", detail)
            ran.append((job.name, "success", detail))
            _logger.info("board daily job %s: %s", job.name, detail)
    return JobsReport(et_date=day, due=tuple(job.name for job in pending), ran=tuple(ran))


async def _finish(
    engine: Engine, job: str, day: date, now: datetime, status: JobStatus, detail: str,
) -> None:
    await asyncio.to_thread(
        mark_finished, engine, job=job, day=day, now=now, status=status, detail=detail,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
