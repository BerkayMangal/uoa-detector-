"""Alfa Board daily closes (Phase 5.2.D0).

Contract ``docs/phase-5.2-alfa-board-acceptance.md``:
- §4.1: a post-close daily job fills this table;
- §4.2: ``alfa_daily_close``, key (ticker, day), append-only;
- §7: a delayed item's outcome is the underlying's move since its filing (or
  as-of) date, read from this table. FAZ C outcomes read it too.

Source: ``GET /api/stock/{ticker}/ohlc/1d`` for the board tickers plus SPY,
parsed by ``webapp.ohlc.regular_session_closes``. The payload is newest first
and mixes pre-market, regular and post-market rows; only regular-session rows
are kept, ordered by date.

Append-only rules:
- a stored (ticker, day) row is never updated, deleted or reset;
- a close is stored only once its session is over (``is_final_close``). During
  the session the newest regular row is an intraday price, and storing it would
  freeze a wrong close forever;
- a day whose payload rows disagree on the close is skipped, not guessed.

UW error policy:
- NotFound (404/422): no data for that ticker;
- RateLimit, Transient, CircuitBreakerOpen: that ticker is degraded and the job
  continues with the next one;
- DailyLimit and Auth: propagate to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, Float, String, Table, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.db import AlfaBase, session_factory
from webapp.ohlc import RegularClose, regular_session_closes

_logger = logging.getLogger(__name__)

BENCHMARK_TICKER: Final = "SPY"
OHLC_DAILY_PATH: Final = "/api/stock/{ticker}/ohlc/1d"

_ET: Final = ZoneInfo("America/New_York")
# Exchange constant, not a tunable cutoff: the US regular session ends at 16:00 ET.
_SESSION_END_ET: Final = time(16, 0)

FetchStatus = Literal["ok", "no_data", "degraded"]


class AlfaDailyClose(AlfaBase):
    """One regular-session close per (ticker, day). Append-only."""

    __tablename__ = "alfa_daily_close"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_daily_close_tables(engine: Engine) -> None:
    """Create ``alfa_daily_close`` when missing. Never alters or drops anything."""
    cast(Table, AlfaDailyClose.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosePoint:
    day: date
    close: float


@dataclass(frozen=True)
class CloseMove:
    """A move between two stored closes; ``start``/``end`` are the rows actually used."""

    start: ClosePoint
    end: ClosePoint
    pct: float  # percent units: 1.5 means +1.5%


def close_on_or_before(closes: Iterable[ClosePoint], day: date) -> ClosePoint | None:
    """The newest close dated on or before ``day``. Input order does not matter."""
    best: ClosePoint | None = None
    for point in closes:
        if point.day <= day and (best is None or point.day > best.day):
            best = point
    return best


def pct_move_between(closes: Sequence[ClosePoint], start: date, end: date) -> CloseMove | None:
    """Percent move from the close on or before ``start`` to the close on or before ``end``.

    None when ``end`` precedes ``start``, when either close is missing, or when
    a close is not positive. When both dates resolve to the same stored row the
    move is 0.0 over zero sessions; callers that need a real session in between
    compare ``end.day`` with ``start.day``.
    """
    if end < start:
        return None
    first = close_on_or_before(closes, start)
    last = close_on_or_before(closes, end)
    if first is None or last is None or first.close <= 0 or last.close <= 0:
        return None
    return CloseMove(start=first, end=last, pct=(last.close / first.close - 1.0) * 100.0)


def is_final_close(day: date, now: datetime) -> bool:
    """True when the regular session of ``day`` has ended as of ``now`` (event time, D9)."""
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    local = now.astimezone(_ET)
    if day < local.date():
        return True
    return day == local.date() and local.time() >= _SESSION_END_ET


def job_tickers(tickers: Iterable[str]) -> tuple[str, ...]:
    """Upper-cased, de-duplicated tickers in input order, with SPY appended when absent."""
    out: list[str] = []
    for raw in (*tickers, BENCHMARK_TICKER):
        symbol = raw.strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return tuple(out)


# ---------------------------------------------------------------------------
# Daily job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyCloseTickerResult:
    ticker: str
    status: FetchStatus
    inserted: int = 0
    already_stored: int = 0
    not_final: int = 0  # closes whose session had not ended at ``now``
    ambiguous_days: int = 0  # days whose payload rows disagree on the close


@dataclass(frozen=True)
class DailyCloseJobResult:
    tickers: tuple[DailyCloseTickerResult, ...]

    @property
    def degraded(self) -> tuple[str, ...]:
        return tuple(r.ticker for r in self.tickers if r.status == "degraded")


async def run_daily_close_job(
    client: UnusualWhalesClient,
    engine: Engine,
    tickers: Iterable[str],
    *,
    now: datetime,
) -> DailyCloseJobResult:
    """Fetch ohlc/1d for every ticker plus SPY and append the missing final closes.

    One request per ticker. DailyLimit and Auth errors propagate; rows already
    committed for earlier tickers stay.
    """
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    ensure_daily_close_tables(engine)
    factory = session_factory(engine)
    results = [
        await _refresh_ticker(client, factory, ticker, now=now) for ticker in job_tickers(tickers)
    ]
    return DailyCloseJobResult(tickers=tuple(results))


def load_closes(engine: Engine, ticker: str) -> tuple[ClosePoint, ...]:
    """Every stored close of ``ticker``, oldest first. Read-only."""
    stmt = (
        select(AlfaDailyClose.day, AlfaDailyClose.close)
        .where(AlfaDailyClose.ticker == ticker.strip().upper())
        .order_by(AlfaDailyClose.day)
    )
    with session_factory(engine)() as session:
        rows = session.execute(stmt).all()
    return tuple(ClosePoint(day=day, close=close) for day, close in rows)


def load_closes_by_ticker(
    engine: Engine, tickers: Sequence[str],
) -> dict[str, tuple[ClosePoint, ...]]:
    """Stored closes of every requested ticker in ONE query, oldest first per ticker. Read-only."""
    symbols = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    if not symbols:
        return {}
    stmt = (
        select(AlfaDailyClose.ticker, AlfaDailyClose.day, AlfaDailyClose.close)
        .where(AlfaDailyClose.ticker.in_(symbols))
        .order_by(AlfaDailyClose.ticker, AlfaDailyClose.day)
    )
    grouped: dict[str, list[ClosePoint]] = {symbol: [] for symbol in symbols}
    with session_factory(engine)() as session:
        for ticker, day, close in session.execute(stmt).all():
            grouped.setdefault(ticker, []).append(ClosePoint(day=day, close=close))
    return {ticker: tuple(points) for ticker, points in grouped.items()}


async def _refresh_ticker(
    client: UnusualWhalesClient,
    factory: sessionmaker[Session],
    ticker: str,
    *,
    now: datetime,
) -> DailyCloseTickerResult:
    try:
        payload = await client.request_json(OHLC_DAILY_PATH.format(ticker=ticker))
    except UnusualWhalesNotFoundError:
        return DailyCloseTickerResult(ticker=ticker, status="no_data")
    except UnusualWhalesDailyLimitError:
        raise
    except (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError) as exc:
        _logger.warning("daily close fetch degraded for %s: %s", ticker, exc)
        return DailyCloseTickerResult(ticker=ticker, status="degraded")

    closes, ambiguous = _unique_positive_closes(regular_session_closes(payload.get("data")))
    if not closes:
        return DailyCloseTickerResult(ticker=ticker, status="no_data", ambiguous_days=ambiguous)
    final = [point for point in closes if is_final_close(point.day, now)]
    inserted, existing = _store(factory, ticker, final, fetched_at=now.astimezone(UTC))
    return DailyCloseTickerResult(
        ticker=ticker,
        status="ok",
        inserted=inserted,
        already_stored=existing,
        not_final=len(closes) - len(final),
        ambiguous_days=ambiguous,
    )


def _unique_positive_closes(bars: Sequence[RegularClose]) -> tuple[list[ClosePoint], int]:
    """Positive closes, one per day, oldest first, plus the number of conflicting days."""
    by_day: dict[date, set[float]] = {}
    for bar in bars:
        if bar.close is None or bar.close <= 0:
            continue
        by_day.setdefault(bar.day, set()).add(bar.close)
    unique = [
        ClosePoint(day=day, close=next(iter(values)))
        for day, values in sorted(by_day.items())
        if len(values) == 1
    ]
    return unique, sum(1 for values in by_day.values() if len(values) > 1)


def _store(
    factory: sessionmaker[Session],
    ticker: str,
    closes: Sequence[ClosePoint],
    *,
    fetched_at: datetime,
) -> tuple[int, int]:
    if not closes:
        return 0, 0
    try:
        return _insert_missing(factory, ticker, closes, fetched_at)
    except IntegrityError:
        # Another writer stored some of these keys between our read and commit.
        _logger.warning("concurrent write on alfa_daily_close for %s; re-reading once", ticker)
        return _insert_missing(factory, ticker, closes, fetched_at)


def _insert_missing(
    factory: sessionmaker[Session],
    ticker: str,
    closes: Sequence[ClosePoint],
    fetched_at: datetime,
) -> tuple[int, int]:
    """Insert the closes whose (ticker, day) is not stored yet. Existing rows are untouched."""
    with factory() as session:
        stored = set(
            session.execute(
                select(AlfaDailyClose.day).where(AlfaDailyClose.ticker == ticker),
            ).scalars(),
        )
        fresh = [point for point in closes if point.day not in stored]
        session.add_all(
            AlfaDailyClose(ticker=ticker, day=p.day, close=p.close, fetched_at=fetched_at)
            for p in fresh
        )
        session.commit()
    return len(fresh), len(closes) - len(fresh)
