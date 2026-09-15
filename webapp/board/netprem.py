"""Net-premium tape for the Alfa Board ``Akış`` family (Phase 5.2.A3).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.2 (``alfa_net_prem``:
rebuildable, upsert), §4.4 (request budget), §5 A3 ("Akış") and §5 B3 (the
since-print sums read this table).

Endpoint, live-probed 2026-09-15 (``alfa_disc/probe_chain_nbbo.md``):

- ``GET /api/stock/{ticker}/net-prem-ticks`` returns the whole trading day so
  far: one row per minute, oldest first, times in UTC.
  - ``net_call_premium`` and ``net_put_premium`` are signed USD strings: the
    ask-side premium minus the bid-side premium of THAT minute. They are
    increments, not running totals.
  - Volumes are integers and ``net_delta`` is a string.
  - The last row is the minute in progress. The next call revises it, so rows
    are upserted by minute.

Storage. One row per (ticker, trade_date, tape_time). The contract keys the
table by (ticker, trade_date) and asks for per-minute rows next to the day
totals. Per-minute rows cannot share a (ticker, trade_date) key, so the minute
is part of the key, and a day total is the SUM over (ticker, trade_date).

Freshness. A fetch stamps ``fetched_at`` on every row it inserts or changes and
on the newest minute, so ``max(fetched_at)`` per (ticker, trade_date) is the
time of the last successful fetch.

Error handling (UW response rules):

- ``UnusualWhalesNotFoundError`` means no data; nothing is written.
- Rate limit, transient and open-breaker errors mark the fetch degraded;
  nothing is written, so the stored tape simply ages.
- ``UnusualWhalesDailyLimitError`` and other ``UnusualWhalesAuthError``
  propagate.
- A payload that is not ``{"data": [...]}``, or any row without a parseable
  minute or premium, degrades the whole fetch. Summing a tape with a silently
  dropped minute would misstate the day total.

Page renders only call ``read_tape_summaries`` and ``read_net_premium_since``,
which read the database.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, Float, Integer, String, Table, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.db import AlfaBase

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.direction import Direction

_logger = logging.getLogger(__name__)

NET_PREM_TICKS_PATH: Final = "/api/stock/{ticker}/net-prem-ticks"

_ET: Final = ZoneInfo("America/New_York")
_ISO_DATE_LENGTH: Final = len("YYYY-MM-DD")
_DEGRADABLE = (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError)
_VALUE_FIELDS: Final = (
    "net_call_premium", "net_put_premium", "net_call_volume", "net_put_volume",
    "call_volume", "put_volume", "net_delta",
)


class AlfaNetPrem(AlfaBase):
    """``alfa_net_prem``: one net-premium minute per (ticker, trade_date, tape_time)."""

    __tablename__ = "alfa_net_prem"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    tape_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    net_call_premium: Mapped[float] = mapped_column(Float, nullable=False)
    net_put_premium: Mapped[float] = mapped_column(Float, nullable=False)
    net_call_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_put_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    put_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def ensure_netprem_tables(engine: Engine) -> None:
    """Create ``alfa_net_prem`` if missing. Never alters or drops anything."""
    cast("Table", AlfaNetPrem.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Parsing (numbers arrive as strings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TapeMinute:
    """One minute of the tape: increments for that minute only."""

    trade_date: date
    tape_time: datetime
    net_call_premium: float
    net_put_premium: float
    net_call_volume: int | None
    net_put_volume: int | None
    call_volume: int | None
    put_volume: int | None
    net_delta: float | None


@dataclass(frozen=True)
class TapeFetch:
    ticker: str
    minutes: tuple[TapeMinute, ...]  # oldest first; empty when degraded or no data
    degraded: bool


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _count(raw: object) -> int | None:
    value = _number(raw)
    if value is None or not value.is_integer():
        return None
    return int(value)


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _trade_date(raw: object, tape_time: datetime) -> date:
    if isinstance(raw, str) and len(raw.strip()) >= _ISO_DATE_LENGTH:
        try:
            return date.fromisoformat(raw.strip()[:_ISO_DATE_LENGTH])
        except ValueError:
            pass
    return tape_time.astimezone(_ET).date()


def parse_net_prem_ticks(payload: object) -> tuple[TapeMinute, ...]:
    """Every minute of the payload, oldest first; a repeated minute keeps its last row.

    Raises ``ValueError`` when the payload is not ``{"data": [...]}`` or a row
    has no parseable ``tape_time``, ``net_call_premium`` or ``net_put_premium``.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        msg = "expected {'data': [...]}"
        raise ValueError(msg)
    minutes: dict[datetime, TapeMinute] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            msg = f"net-prem row {index} is not an object"
            raise ValueError(msg)
        tape_time = _timestamp(row.get("tape_time"))
        call_premium = _number(row.get("net_call_premium"))
        put_premium = _number(row.get("net_put_premium"))
        if tape_time is None or call_premium is None or put_premium is None:
            msg = f"net-prem row {index} has no parseable tape_time or net premium"
            raise ValueError(msg)
        minutes[tape_time] = TapeMinute(
            trade_date=_trade_date(row.get("date"), tape_time),
            tape_time=tape_time,
            net_call_premium=call_premium,
            net_put_premium=put_premium,
            net_call_volume=_count(row.get("net_call_volume")),
            net_put_volume=_count(row.get("net_put_volume")),
            call_volume=_count(row.get("call_volume")),
            put_volume=_count(row.get("put_volume")),
            net_delta=_number(row.get("net_delta")),
        )
    return tuple(minutes[key] for key in sorted(minutes))


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class _JsonClient(Protocol):
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


async def fetch_net_prem_ticks(client: _JsonClient, ticker: str) -> TapeFetch:
    """One ``net-prem-ticks`` call: the ticker's tape for the current trading day."""
    symbol = ticker.strip().upper()
    path = NET_PREM_TICKS_PATH.format(ticker=symbol)
    try:
        payload = await client.request_json(path)
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        return TapeFetch(ticker=symbol, minutes=(), degraded=False)
    except _DEGRADABLE as exc:
        _logger.warning("alfa tape: net-prem-ticks for %s degraded (%s)", symbol, type(exc).__name__)
        return TapeFetch(ticker=symbol, minutes=(), degraded=True)
    try:
        return TapeFetch(ticker=symbol, minutes=parse_net_prem_ticks(payload), degraded=False)
    except ValueError as exc:
        _logger.warning("alfa tape: unexpected net-prem-ticks payload for %s (%s)", symbol, exc)
        return TapeFetch(ticker=symbol, minutes=(), degraded=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert_tape(
    engine: Engine, ticker: str, minutes: Sequence[TapeMinute], *, fetched_at: datetime,
) -> int:
    """Insert new minutes and update revised ones; returns the rows inserted or changed.

    The newest minute always gets the new ``fetched_at``, even when unchanged,
    so the table records when the tape was last fetched.
    """
    if not minutes:
        return 0
    symbol = ticker.strip().upper()
    stamp = _as_utc(fetched_at)
    newest = max(_as_utc(m.tape_time) for m in minutes)
    written = 0
    with Session(engine) as session:
        existing: dict[tuple[date, datetime], AlfaNetPrem] = {
            (row.trade_date, _as_utc(row.tape_time)): row
            for row in session.execute(
                select(AlfaNetPrem).where(
                    AlfaNetPrem.ticker == symbol,
                    AlfaNetPrem.trade_date.in_({m.trade_date for m in minutes}),
                ),
            ).scalars()
        }
        for minute in minutes:
            moment = _as_utc(minute.tape_time)
            values = {name: getattr(minute, name) for name in _VALUE_FIELDS}
            row = existing.get((minute.trade_date, moment))
            if row is None:
                session.add(
                    AlfaNetPrem(
                        ticker=symbol, trade_date=minute.trade_date, tape_time=moment,
                        fetched_at=stamp, **values,
                    ),
                )
                written += 1
                continue
            changed = any(getattr(row, name) != value for name, value in values.items())
            if changed:
                for name, value in values.items():
                    setattr(row, name, value)
                written += 1
            if changed or moment == newest:
                row.fetched_at = stamp
        session.commit()
    return written


@dataclass(frozen=True)
class TapeSummary:
    """Summed tape for one (ticker, trade_date), optionally from a start minute."""

    ticker: str
    trade_date: date
    net_call_premium: float
    net_put_premium: float
    minutes: int
    first_tape_time: datetime
    last_tape_time: datetime
    fetched_at: datetime  # the last fetch that touched these rows

    @property
    def bullish_net_premium(self) -> float:
        """Net call premium minus net put premium: bought calls and sold puts count up."""
        return self.net_call_premium - self.net_put_premium

    def net_premium_for(self, direction: Direction) -> float:
        """Net aggressor premium in ``direction`` (negative: the opposite side dominates)."""
        return self.bullish_net_premium if direction == "up" else -self.bullish_net_premium


def _summary_columns() -> tuple[Any, ...]:
    return (
        AlfaNetPrem.ticker,
        AlfaNetPrem.trade_date,
        func.sum(AlfaNetPrem.net_call_premium),
        func.sum(AlfaNetPrem.net_put_premium),
        func.count(),
        func.min(AlfaNetPrem.tape_time),
        func.max(AlfaNetPrem.tape_time),
        func.max(AlfaNetPrem.fetched_at),
    )


def _summary(row: Any) -> TapeSummary | None:
    ticker, trade_date, call_sum, put_sum, count, first, last, fetched = row
    if not count or first is None or last is None or fetched is None:
        return None
    return TapeSummary(
        ticker=ticker,
        trade_date=trade_date,
        net_call_premium=float(call_sum or 0.0),
        net_put_premium=float(put_sum or 0.0),
        minutes=int(count),
        first_tape_time=_as_utc(first),
        last_tape_time=_as_utc(last),
        fetched_at=_as_utc(fetched),
    )


def read_tape_summaries(
    engine: Engine, keys: Iterable[tuple[str, date]],
) -> dict[tuple[str, date], TapeSummary]:
    """Whole-day summaries for each wanted (ticker, trade_date) that has rows."""
    wanted = {(ticker.strip().upper(), day) for ticker, day in keys}
    if not wanted:
        return {}
    stmt = (
        select(*_summary_columns())
        .where(
            AlfaNetPrem.ticker.in_({ticker for ticker, _day in wanted}),
            AlfaNetPrem.trade_date.in_({day for _ticker, day in wanted}),
        )
        .group_by(AlfaNetPrem.ticker, AlfaNetPrem.trade_date)
    )
    out: dict[tuple[str, date], TapeSummary] = {}
    with Session(engine) as session:
        for row in session.execute(stmt):
            summary = _summary(row)
            if summary is not None and (summary.ticker, summary.trade_date) in wanted:
                out[(summary.ticker, summary.trade_date)] = summary
    return out


def read_net_premium_since(
    engine: Engine, ticker: str, trade_date: date, since: datetime,
) -> TapeSummary | None:
    """The tape summed from the minute containing ``since`` onwards, or None without rows."""
    start = _as_utc(since).replace(second=0, microsecond=0)
    stmt = (
        select(*_summary_columns())
        .where(
            AlfaNetPrem.ticker == ticker.strip().upper(),
            AlfaNetPrem.trade_date == trade_date,
            AlfaNetPrem.tape_time >= start,
        )
        .group_by(AlfaNetPrem.ticker, AlfaNetPrem.trade_date)
    )
    with Session(engine) as session:
        row = session.execute(stmt).first()
    return None if row is None else _summary(row)
