"""Option quotes and top-K exit depth for the Alfa Board (Phase 5.2.A2).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1, §4.2 (``alfa_quote``
and ``alfa_contract_depth``: rebuildable, upsert), §4.4 (request budget) and
§5 A2; decisions P14 (``option-contracts`` instead of ``option-chains``), P16
and P17.

Two Unusual Whales endpoints, live-probed on 2026-09-15
(``alfa_disc/probe_chain_nbbo.md`` and the trimmed samples in the tests):

- ``GET /api/stock/{ticker}/option-contracts?option_symbol[]=...``: one call
  per underlying for any set of contracts. It carries ``nbbo_bid``,
  ``nbbo_ask``, ``last_price``, ``volume``, ``open_interest`` and
  ``last_tape_time``, but no sizes and no quote timestamp.
  - Prices arrive as strings.
  - NBBO is null exactly when today's volume is 0.
  - An untraded row carries a placeholder ``last_tape_time`` around
    ``10:30 UTC`` (06:30 ET; seen ``10:30:02Z`` and ``10:30:34Z``), before any
    option session. A tape time is kept only when volume is positive and the
    time falls inside ``sources.market_hours.is_market_open``.
  - A symbol UW does not know is dropped silently, so the requested symbols
    are diffed against the returned ones. A missing symbol is stored with
    ``returned = False`` (the board's ``yok``).
- ``GET /api/option-contract/{symbol}/flow?limit=1``: the newest print of a
  contract. Its ``nbbo_bid_size``/``nbbo_ask_size`` and ``nbbo_*_time`` are as
  of that print, not live; the board labels them ``son işlem anında``.

Error handling (UW response rules):

- ``UnusualWhalesNotFoundError`` means no data: every requested symbol is
  not returned, or there is no depth.
- Rate limit, transient and open-breaker errors mark the fetch degraded.
  Nothing is written, so the previous row simply ages.
- ``UnusualWhalesDailyLimitError`` and other ``UnusualWhalesAuthError``
  propagate to the caller.
- An unexpected payload shape is degraded as well, never mistaken for
  "not returned".

Persistence (review RT-1). ``upsert_quotes`` and ``upsert_depths`` write a
whole batch as one ``INSERT ... ON CONFLICT DO UPDATE`` statement (PostgreSQL
and SQLite), never one ``SELECT`` plus one write per contract. The refresher
shares the web server's event loop, and the production database is remote, so
the statement count per cycle must not grow with the number of contracts.

Nothing here runs at page render except ``read_quotes``, ``read_depths`` and
``read_board_quotes``, which only read the database.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Table, bindparam, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.sources.market_hours import is_market_open
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.db import AlfaBase
from webapp.board.tradability import DepthView, QuoteView

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.aggregate import BoardRow, ContractSummary

_logger = logging.getLogger(__name__)

OPTION_CONTRACTS_PATH: Final = "/api/stock/{ticker}/option-contracts"
CONTRACT_FLOW_PATH: Final = "/api/option-contract/{symbol}/flow"
SYMBOL_PARAM: Final = "option_symbol[]"

_STRIKE_SCALE: Final = Decimal(1000)
_STRIKE_UNITS_LIMIT: Final = Decimal(10) ** 8  # OCC strike field: 8 digits
_DEGRADABLE = (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError)
# Bound parameters per upsert statement. SQLite's default SQLITE_MAX_VARIABLE_NUMBER
# (3.32+) is 32,766 and PostgreSQL allows 65,535, so a batch stays under both.
_MAX_BIND_PARAMETERS: Final = 32766


class AlfaQuote(AlfaBase):
    """``alfa_quote``: the latest option-contracts row per contract (rebuildable)."""

    __tablename__ = "alfa_quote"

    option_symbol: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    nbbo_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    nbbo_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tape_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    returned: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AlfaContractDepth(AlfaBase):
    """``alfa_contract_depth``: NBBO sizes and times at the last print (top-K only)."""

    __tablename__ = "alfa_contract_depth"

    option_symbol: Mapped[str] = mapped_column(String, primary_key=True)
    nbbo_bid_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nbbo_ask_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nbbo_bid_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nbbo_ask_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def ensure_quotes_tables(engine: Engine) -> None:
    """Create both quote tables if missing. Never alters or drops anything."""
    for model in (AlfaQuote, AlfaContractDepth):
        cast("Table", model.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def occ_symbol(ticker: str, expiry: date, option_type: str, strike: Decimal) -> str | None:
    """OCC-style symbol (root + YYMMDD + C/P + strike × 1000 in 8 digits), or None.

    Built only when no UW chain was recorded (legacy rows, journal legs). UW
    drops a symbol it does not list, and the refresher then records ``yok``.
    """
    root = "".join(ch for ch in ticker.upper() if ch.isascii() and ch.isalnum())
    if not root or option_type not in ("call", "put"):
        return None
    try:
        units = strike * _STRIKE_SCALE
        integral = units == units.to_integral_value()
    except InvalidOperation:
        return None
    if strike <= 0 or not integral or units >= _STRIKE_UNITS_LIMIT:
        return None
    kind = "C" if option_type == "call" else "P"
    return f"{root}{expiry:%y%m%d}{kind}{int(units):08d}"


def contract_symbol(ticker: str, contract: ContractSummary) -> str | None:
    """A contract's quote symbol: the UW chain recorded on its prints, else OCC."""
    key = contract.key
    return contract.option_chain or occ_symbol(ticker, key.expiry, key.option_type, key.strike)


def dominant_symbol(row: BoardRow) -> str | None:
    """The quote symbol of a row's dominant contract."""
    return contract_symbol(row.ticker, row.dominant)


# ---------------------------------------------------------------------------
# Parsing (numbers arrive as strings)
# ---------------------------------------------------------------------------


class _ShapeError(ValueError):
    """The payload is not the probed ``{"data": [...]}`` shape."""


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


def _price(raw: object) -> float | None:
    value = _number(raw)
    return value if value is not None and value >= 0 else None


def _count(raw: object) -> int | None:
    value = _number(raw)
    if value is None or value < 0 or not value.is_integer():
        return None
    return int(value)


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _tape_time(raw: object, volume: int | None) -> datetime | None:
    parsed = _timestamp(raw)
    if parsed is None or volume is None or volume <= 0 or not is_market_open(parsed):
        return None
    return parsed


# Any (D12 adapter boundary): rows of the untyped UW JSON payload; callers isinstance-check each row.
def _data_rows(payload: object) -> list[Any]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        msg = "expected {'data': [...]}"
        raise _ShapeError(msg)
    return rows


def _unique_symbols(symbols: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))


@dataclass(frozen=True)
class QuoteSnapshot:
    option_symbol: str
    ticker: str
    nbbo_bid: float | None
    nbbo_ask: float | None
    last_price: float | None
    volume: int | None
    open_interest: int | None
    last_tape_time: datetime | None
    returned: bool


@dataclass(frozen=True)
class QuoteFetch:
    quotes: tuple[QuoteSnapshot, ...]  # one per requested symbol; empty when degraded
    degraded: bool


@dataclass(frozen=True)
class DepthSnapshot:
    option_symbol: str
    nbbo_bid_size: int | None
    nbbo_ask_size: int | None
    nbbo_bid_time: datetime | None
    nbbo_ask_time: datetime | None


@dataclass(frozen=True)
class DepthFetch:
    depth: DepthSnapshot | None
    degraded: bool


def parse_option_contract_rows(
    payload: object, ticker: str, requested: Sequence[str],
) -> tuple[QuoteSnapshot, ...]:
    """One snapshot per requested symbol; a symbol UW did not return gets ``returned=False``.

    Raises ``ValueError`` when the payload is not ``{"data": [...]}``.
    """
    wanted = _unique_symbols(requested)
    wanted_set = set(wanted)
    # Any (D12 adapter boundary): raw UW JSON row values, parsed defensively below.
    found: dict[str, dict[str, Any]] = {}
    for row in _data_rows(payload):
        if not isinstance(row, dict):
            continue
        symbol = row.get("option_symbol")
        if not isinstance(symbol, str):
            continue
        key = symbol.strip().upper()
        if key in wanted_set and key not in found:
            found[key] = row
    out: list[QuoteSnapshot] = []
    for symbol in wanted:
        row = found.get(symbol)
        if row is None:
            out.append(
                QuoteSnapshot(
                    option_symbol=symbol, ticker=ticker, nbbo_bid=None, nbbo_ask=None,
                    last_price=None, volume=None, open_interest=None, last_tape_time=None,
                    returned=False,
                ),
            )
            continue
        volume = _count(row.get("volume"))
        out.append(
            QuoteSnapshot(
                option_symbol=symbol,
                ticker=ticker,
                nbbo_bid=_price(row.get("nbbo_bid")),
                nbbo_ask=_price(row.get("nbbo_ask")),
                last_price=_price(row.get("last_price")),
                volume=volume,
                open_interest=_count(row.get("open_interest")),
                last_tape_time=_tape_time(row.get("last_tape_time"), volume),
                returned=True,
            ),
        )
    return tuple(out)


def parse_contract_flow(payload: object, symbol: str) -> DepthSnapshot | None:
    """Sizes and quote times from the newest print, or None when there is none.

    Raises ``ValueError`` when the payload is not ``{"data": [...]}``.
    """
    rows = _data_rows(payload)
    if not rows or not isinstance(rows[0], dict):
        return None
    newest = rows[0]
    return DepthSnapshot(
        option_symbol=symbol.strip().upper(),
        nbbo_bid_size=_count(newest.get("nbbo_bid_size")),
        nbbo_ask_size=_count(newest.get("nbbo_ask_size")),
        nbbo_bid_time=_timestamp(newest.get("nbbo_bid_time")),
        nbbo_ask_time=_timestamp(newest.get("nbbo_ask_time")),
    )


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


class _JsonClient(Protocol):
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


async def fetch_contract_quotes(
    client: _JsonClient, ticker: str, symbols: Sequence[str],
) -> QuoteFetch:
    """One ``option-contracts`` call for ``symbols`` of one underlying."""
    wanted = _unique_symbols(symbols)
    if not wanted:
        return QuoteFetch(quotes=(), degraded=False)
    path = OPTION_CONTRACTS_PATH.format(ticker=ticker.upper())
    try:
        payload = await client.request_json(path, params={SYMBOL_PARAM: wanted})
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        return QuoteFetch(quotes=parse_option_contract_rows({"data": []}, ticker, wanted), degraded=False)
    except _DEGRADABLE as exc:
        _logger.warning("alfa quotes: option-contracts for %s degraded (%s)", ticker, type(exc).__name__)
        return QuoteFetch(quotes=(), degraded=True)
    try:
        return QuoteFetch(quotes=parse_option_contract_rows(payload, ticker, wanted), degraded=False)
    except ValueError as exc:
        _logger.warning("alfa quotes: unexpected option-contracts payload for %s (%s)", ticker, exc)
        return QuoteFetch(quotes=(), degraded=True)


async def fetch_contract_depth(client: _JsonClient, symbol: str) -> DepthFetch:
    """One ``/flow?limit=1`` call: NBBO sizes and times at the contract's last print."""
    path = CONTRACT_FLOW_PATH.format(symbol=symbol.strip().upper())
    try:
        payload = await client.request_json(path, params={"limit": 1})
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        return DepthFetch(depth=None, degraded=False)
    except _DEGRADABLE as exc:
        _logger.warning("alfa quotes: flow for %s degraded (%s)", symbol, type(exc).__name__)
        return DepthFetch(depth=None, degraded=True)
    try:
        return DepthFetch(depth=parse_contract_flow(payload, symbol), degraded=False)
    except ValueError as exc:
        _logger.warning("alfa quotes: unexpected flow payload for %s (%s)", symbol, exc)
        return DepthFetch(depth=None, degraded=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _upsert_statement(dialect_name: str, table: Table, key: str, rows: Sequence[dict[str, object]]) -> Any:
    """One ``INSERT ... ON CONFLICT (key) DO UPDATE`` for ``rows``, or None for another dialect.

    Returns the dialect's executable insert construct (typed ``Any``: the
    PostgreSQL and SQLite insert classes share no common typed base).
    """
    columns = list(rows[0])
    if dialect_name == "postgresql":
        pg_stmt = postgresql.insert(table).values(list(rows))
        return pg_stmt.on_conflict_do_update(
            index_elements=[table.c[key]],
            set_={name: pg_stmt.excluded[name] for name in columns if name != key},
        )
    if dialect_name == "sqlite":
        lite_stmt = sqlite.insert(table).values(list(rows))
        return lite_stmt.on_conflict_do_update(
            index_elements=[table.c[key]],
            set_={name: lite_stmt.excluded[name] for name in columns if name != key},
        )
    return None


def _upsert_rows(engine: Engine, table: Table, key: str, rows: Sequence[dict[str, object]]) -> int:
    """Insert or update ``rows`` by primary key ``key``: one statement per batch, not per row."""
    if not rows:
        return 0
    batch = max(1, _MAX_BIND_PARAMETERS // len(rows[0]))
    with engine.begin() as conn:
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            stmt = _upsert_statement(engine.dialect.name, table, key, chunk)
            if stmt is not None:
                conn.execute(stmt)
                continue
            # Another dialect: one SELECT for the batch's keys, then batched INSERT and UPDATE.
            keys = [row[key] for row in chunk]
            existing = set(conn.execute(select(table.c[key]).where(table.c[key].in_(keys))).scalars())
            fresh = [row for row in chunk if row[key] not in existing]
            if fresh:
                conn.execute(table.insert(), fresh)
            stale = [{**row, "_key": row[key]} for row in chunk if row[key] in existing]
            if stale:
                conn.execute(update(table).where(table.c[key] == bindparam("_key")), stale)
    return len(rows)


def upsert_quotes(engine: Engine, quotes: Iterable[QuoteSnapshot], *, fetched_at: datetime) -> int:
    rows: dict[str, dict[str, object]] = {}
    for q in quotes:
        rows[q.option_symbol] = {
            "option_symbol": q.option_symbol,
            "ticker": q.ticker,
            "nbbo_bid": q.nbbo_bid,
            "nbbo_ask": q.nbbo_ask,
            "last_price": q.last_price,
            "volume": q.volume,
            "open_interest": q.open_interest,
            "last_tape_time": q.last_tape_time,
            "fetched_at": fetched_at,
            "returned": q.returned,
        }
    return _upsert_rows(engine, cast("Table", AlfaQuote.__table__), "option_symbol", list(rows.values()))


def upsert_depths(engine: Engine, depths: Iterable[DepthSnapshot], *, fetched_at: datetime) -> int:
    rows: dict[str, dict[str, object]] = {}
    for d in depths:
        rows[d.option_symbol] = {
            "option_symbol": d.option_symbol,
            "nbbo_bid_size": d.nbbo_bid_size,
            "nbbo_ask_size": d.nbbo_ask_size,
            "nbbo_bid_time": d.nbbo_bid_time,
            "nbbo_ask_time": d.nbbo_ask_time,
            "fetched_at": fetched_at,
        }
    return _upsert_rows(engine, cast("Table", AlfaContractDepth.__table__), "option_symbol", list(rows.values()))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def read_quotes(engine: Engine, symbols: Iterable[str]) -> dict[str, QuoteView]:
    wanted = _unique_symbols(symbols)
    if not wanted:
        return {}
    out: dict[str, QuoteView] = {}
    with Session(engine) as session:
        rows = session.execute(select(AlfaQuote).where(AlfaQuote.option_symbol.in_(wanted))).scalars().all()
        for r in rows:
            fetched = _utc(r.fetched_at)
            if fetched is None:
                continue
            out[r.option_symbol] = QuoteView(
                option_symbol=r.option_symbol,
                nbbo_bid=r.nbbo_bid,
                nbbo_ask=r.nbbo_ask,
                volume=r.volume,
                last_tape_time=_utc(r.last_tape_time),
                fetched_at=fetched,
                returned=r.returned,
            )
    return out


def read_depths(engine: Engine, symbols: Iterable[str]) -> dict[str, DepthView]:
    wanted = _unique_symbols(symbols)
    if not wanted:
        return {}
    out: dict[str, DepthView] = {}
    with Session(engine) as session:
        rows = session.execute(
            select(AlfaContractDepth).where(AlfaContractDepth.option_symbol.in_(wanted)),
        ).scalars().all()
        for r in rows:
            fetched = _utc(r.fetched_at)
            if fetched is None:
                continue
            times = [t for t in (_utc(r.nbbo_bid_time), _utc(r.nbbo_ask_time)) if t is not None]
            out[r.option_symbol] = DepthView(
                option_symbol=r.option_symbol,
                nbbo_bid_size=r.nbbo_bid_size,
                nbbo_ask_size=r.nbbo_ask_size,
                quote_time=max(times) if times else None,
                fetched_at=fetched,
            )
    return out


# The engine whose quote tables are known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_board_quotes(
    engine: Engine, symbols: Sequence[str],
) -> tuple[dict[str, QuoteView], dict[str, DepthView]]:
    """Quotes and depths for ``symbols``; creates the tables once on a fresh database."""
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_quotes_tables(engine)
        _tables_ready_for[:] = [engine]
    return read_quotes(engine, symbols), read_depths(engine, symbols)
