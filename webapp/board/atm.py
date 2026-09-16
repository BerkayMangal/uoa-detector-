"""ATM straddle data layer for the Alfa Board (Phase 5.2.B2a, contract §6 B2).

Two Unusual Whales endpoints feed ``alfa_atm``:

- ``/api/stock/{t}/expiry-breakdown`` once a day. It lists the ticker's expiries
  (live field name ``expires``; the vendor spec says ``expiry``). The list is kept
  in ``alfa_atm_expiry`` so the per-cycle job only asks for listed expiries and a
  restart does not need a second daily call.
- ``/api/stock/{t}/atm-chains?expirations[]=...`` every refresher cycle. Live rows
  carry ``bid``/``ask`` (not in the vendor spec; live wins), ``iv``,
  ``stock_price`` and ``tape_time``. There is one call row and one put row per
  expiry at the same strike, and the strike, expiry and type only exist inside
  the OSI ``option_symbol``, so it is parsed here.

Both tables are rebuildable market data (upsert). Nothing here renders; the pure
readers return frozen views for ``webapp/board/moves.py`` and the UI.

UW error handling (program rule 9):
- ``UnusualWhalesNotFoundError`` (404/422): that ticker has no data.
- rate limit, transient, open breaker: the ticker is reported degraded and the job
  moves on to the next ticker.
- daily limit and auth errors propagate to the caller.

No numeric cutoff lives here (D8): the number of expiries comes from
``BoardSettings.refresh.atm_expiries``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, Float, Integer, String, delete, func, select
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
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.settings import BoardSettings

EXPIRY_BREAKDOWN_PATH: Final = "/api/stock/{ticker}/expiry-breakdown"
ATM_CHAINS_PATH: Final = "/api/stock/{ticker}/atm-chains"

_ET: Final = ZoneInfo("America/New_York")
_DEGRADED: Final = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)
# OSI symbol as UW prints it: root, YYMMDD, C/P, strike x 1000 in 8 digits.
_OSI: Final = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9.]*?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$",
)
_OSI_STRIKE_SCALE: Final = 1000
_OSI_CENTURY: Final = 2000


class _JsonClient(Protocol):
    """What the ATM jobs need from ``UnusualWhalesClient`` (the refresher passes its own client)."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class AlfaAtmExpiry(AlfaBase):
    """Listed expiries per ticker, from the daily expiry-breakdown call (rebuildable)."""

    __tablename__ = "alfa_atm_expiry"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    expiry: Mapped[date] = mapped_column(Date, primary_key=True)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlfaAtm(AlfaBase):
    """ATM call and put quote per (ticker, expiry), from atm-chains (rebuildable)."""

    __tablename__ = "alfa_atm"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    expiry: Mapped[date] = mapped_column(Date, primary_key=True)
    strike: Mapped[float] = mapped_column(Float)
    stock_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    call_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_tape_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    put_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    put_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_tape_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_atm_tables(engine: Engine) -> None:
    """Create the ATM tables if missing. Never alters or drops anything."""
    for model in (AlfaAtmExpiry, AlfaAtm):
        cast("Table", model.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Reports and views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpiryRefreshReport:
    fetched_at: datetime
    stored: tuple[tuple[str, int], ...]  # (ticker, listed expiries stored)
    no_data: tuple[str, ...]
    degraded: tuple[str, ...]
    requests: int


@dataclass(frozen=True)
class AtmRefreshReport:
    fetched_at: datetime
    stored_rows: int
    no_data: tuple[str, ...]
    degraded: tuple[str, ...]
    no_listed_expiries: tuple[str, ...]
    missing_expiries: tuple[tuple[str, date], ...]  # asked for, UW returned no row
    malformed_rows: int
    requests: int


@dataclass(frozen=True)
class AtmView:
    ticker: str
    expiry: date
    strike: float
    stock_price: float | None
    call_bid: float | None
    call_ask: float | None
    call_iv: float | None
    put_bid: float | None
    put_ask: float | None
    put_iv: float | None
    trade_date: date | None
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def refresh_expiry_breakdown(
    client: _JsonClient,
    sessions: sessionmaker[Session],
    *,
    tickers: Sequence[str],
    settings: BoardSettings,
    now: datetime,
) -> ExpiryRefreshReport:
    """Daily job: replace each ticker's listed expiries from expiry-breakdown.

    ``settings`` is accepted for the uniform job signature; this job has no cutoff.
    A ticker whose call fails keeps its previous list.
    """
    del settings
    fetched_at = _as_utc(now)
    stored: list[tuple[str, int]] = []
    no_data: list[str] = []
    degraded: list[str] = []
    requests = 0
    for ticker in _unique_upper(tickers):
        requests += 1
        try:
            resp = await client.request_json(EXPIRY_BREAKDOWN_PATH.format(ticker=ticker))
        except UnusualWhalesDailyLimitError:
            raise
        except UnusualWhalesNotFoundError:
            no_data.append(ticker)
            continue
        except _DEGRADED:
            degraded.append(ticker)
            continue
        rows = _parse_breakdown(resp.get("data"))
        if not rows:
            no_data.append(ticker)
            continue
        with sessions() as session, session.begin():
            session.execute(delete(AlfaAtmExpiry).where(AlfaAtmExpiry.ticker == ticker))
            for expiry, oi, volume, chains in rows:
                session.add(AlfaAtmExpiry(
                    ticker=ticker, expiry=expiry, open_interest=oi, volume=volume,
                    chains=chains, fetched_at=fetched_at,
                ))
        stored.append((ticker, len(rows)))
    return ExpiryRefreshReport(
        fetched_at=fetched_at, stored=tuple(stored), no_data=tuple(no_data),
        degraded=tuple(degraded), requests=requests,
    )


async def refresh_atm_chains(
    client: _JsonClient,
    sessions: sessionmaker[Session],
    *,
    wanted: Mapping[str, Sequence[date]],
    settings: BoardSettings,
    now: datetime,
) -> AtmRefreshReport:
    """Cycle job: one atm-chains call per ticker for up to ``refresh.atm_expiries`` expiries.

    ``wanted`` maps a ticker to the expiries its board rows need, most important
    first (the dominant contracts' expiries). Each is snapped to a listed expiry;
    free slots are filled with the nearest listed expiries. Rows of expired
    expiries are removed for the ticker.
    """
    fetched_at = _as_utc(now)
    today = fetched_at.astimezone(_ET).date()
    stored_rows = 0
    malformed = 0
    no_data: list[str] = []
    degraded: list[str] = []
    no_listed: list[str] = []
    missing: list[tuple[str, date]] = []
    requests = 0
    merged: dict[str, list[date]] = {}
    for key, expiries in wanted.items():
        cleaned = key.strip().upper()
        if cleaned:
            merged.setdefault(cleaned, []).extend(expiries)
    for ticker, targets in merged.items():
        with sessions() as session:
            listed = load_listed_expiries(session, ticker)
        chosen = select_atm_expiries(
            listed, targets, today=today, max_expiries=settings.refresh.atm_expiries,
        )
        if not chosen:
            no_listed.append(ticker)
            continue
        requests += 1
        try:
            resp = await client.request_json(
                ATM_CHAINS_PATH.format(ticker=ticker),
                params={"expirations[]": [e.isoformat() for e in chosen]},
            )
        except UnusualWhalesDailyLimitError:
            raise
        except UnusualWhalesNotFoundError:
            no_data.append(ticker)
            continue
        except _DEGRADED:
            degraded.append(ticker)
            continue
        pairs, bad = _parse_atm_rows(resp.get("data"), ticker=ticker)
        malformed += bad
        with sessions() as session, session.begin():
            for pair in pairs.values():
                _upsert_atm(session, pair, fetched_at=fetched_at)
                stored_rows += 1
            session.execute(
                delete(AlfaAtm).where(AlfaAtm.ticker == ticker, AlfaAtm.expiry < today),
            )
        missing.extend((ticker, e) for e in chosen if e not in pairs)
        if not pairs:
            no_data.append(ticker)
    return AtmRefreshReport(
        fetched_at=fetched_at, stored_rows=stored_rows, no_data=tuple(no_data),
        degraded=tuple(degraded), no_listed_expiries=tuple(no_listed),
        missing_expiries=tuple(missing), malformed_rows=malformed, requests=requests,
    )


def select_atm_expiries(
    listed: Sequence[date],
    wanted: Sequence[date],
    *,
    today: date,
    max_expiries: int,
) -> tuple[date, ...]:
    """Pick at most ``max_expiries`` listed expiries on or after ``today``.

    Each wanted expiry maps to itself when listed, otherwise to the nearest listed
    expiry (ties go to the earlier date). Remaining slots take the nearest listed
    expiries. The result is sorted and has no repeats.
    """
    future = sorted({e for e in listed if e >= today})
    if not future or max_expiries <= 0:
        return ()
    chosen: list[date] = []
    for target in wanted:
        if len(chosen) >= max_expiries:
            break
        if target < today:
            continue
        snap = min(future, key=lambda e: (abs((e - target).days), e))
        if snap not in chosen:
            chosen.append(snap)
    for expiry in future:
        if len(chosen) >= max_expiries:
            break
        if expiry not in chosen:
            chosen.append(expiry)
    return tuple(sorted(chosen))


# ---------------------------------------------------------------------------
# Pure readers
# ---------------------------------------------------------------------------


def load_listed_expiries(session: Session, ticker: str) -> tuple[date, ...]:
    rows = session.scalars(
        select(AlfaAtmExpiry.expiry)
        .where(AlfaAtmExpiry.ticker == ticker.upper())
        .order_by(AlfaAtmExpiry.expiry),
    )
    return tuple(rows)


def load_atm(session: Session, ticker: str) -> tuple[AtmView, ...]:
    """Every stored ATM row for ``ticker``, sorted by expiry."""
    rows = session.scalars(
        select(AlfaAtm).where(AlfaAtm.ticker == ticker.upper()).order_by(AlfaAtm.expiry),
    )
    return tuple(_view(r) for r in rows)


def _view(row: AlfaAtm) -> AtmView:
    return AtmView(
        ticker=row.ticker, expiry=row.expiry, strike=row.strike, stock_price=row.stock_price,
        call_bid=row.call_bid, call_ask=row.call_ask, call_iv=row.call_iv,
        put_bid=row.put_bid, put_ask=row.put_ask, put_iv=row.put_iv,
        trade_date=row.trade_date, fetched_at=_as_utc(row.fetched_at),
    )


def tickers_needing_expiries(engine: Engine, tickers: Iterable[str], *, today: date) -> list[str]:
    """Tickers, in the given order, whose listed expiries were not fetched on ``today`` (ET).

    The daily ``expiry-breakdown`` call is driven by the stored rows rather than
    by an in-process flag, so a refresher restart does not re-fetch the list and
    a missed day is picked up on the next cycle.
    """
    ordered = _unique_upper(list(tickers))
    if not ordered:
        return []
    with Session(engine) as session:
        rows = session.execute(
            select(AlfaAtmExpiry.ticker, func.max(AlfaAtmExpiry.fetched_at))
            .where(AlfaAtmExpiry.ticker.in_(ordered))
            .group_by(AlfaAtmExpiry.ticker),
        )
        fetched = {
            ticker: _as_utc(stamp).astimezone(_ET).date()
            for ticker, stamp in rows
            if isinstance(stamp, datetime)
        }
    return [t for t in ordered if fetched.get(t) != today]


# The engine whose ATM tables are known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_board_atm(engine: Engine, tickers: Sequence[str]) -> dict[str, tuple[AtmView, ...]]:
    """Stored ATM rows per ticker for the render path: one query, and no UW call.

    Creates the tables once per engine, so a fresh database renders
    ``bilinmiyor`` instead of failing.
    """
    wanted = _unique_upper(list(tickers))
    if not wanted:
        return {}
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_atm_tables(engine)
        _tables_ready_for[:] = [engine]
    out: dict[str, list[AtmView]] = {}
    with Session(engine) as session:
        rows = session.scalars(
            select(AlfaAtm).where(AlfaAtm.ticker.in_(wanted)).order_by(AlfaAtm.ticker, AlfaAtm.expiry),
        )
        for row in rows:
            out.setdefault(row.ticker, []).append(_view(row))
    return {ticker: tuple(views) for ticker, views in out.items()}


# ---------------------------------------------------------------------------
# Parsing (private)
# ---------------------------------------------------------------------------


@dataclass
class _Leg:
    symbol: str
    bid: float | None
    ask: float | None
    iv: float | None
    tape_time: datetime | None


@dataclass
class _Pair:
    ticker: str
    expiry: date
    strike: float
    stock_price: float | None = None
    trade_date: date | None = None
    call: _Leg | None = None
    put: _Leg | None = None


def _parse_breakdown(raw: object) -> list[tuple[date, int | None, int | None, int | None]]:
    out: dict[date, tuple[date, int | None, int | None, int | None]] = {}
    for row in _rows(raw):
        expiry = _day(row.get("expires"))
        if expiry is None:
            continue
        out[expiry] = (
            expiry, _int(row.get("open_interest")), _int(row.get("volume")),
            _int(row.get("chains")),
        )
    return [out[k] for k in sorted(out)]


def _parse_atm_rows(raw: object, *, ticker: str) -> tuple[dict[date, _Pair], int]:
    pairs: dict[date, _Pair] = {}
    malformed = 0
    for row in _rows(raw):
        osi = _parse_osi(row.get("option_symbol"))
        if osi is None:
            malformed += 1
            continue
        expiry, side, strike = osi
        leg = _Leg(
            symbol=str(row["option_symbol"]), bid=_num(row.get("bid")),
            ask=_num(row.get("ask")), iv=_num(row.get("iv")),
            tape_time=_ts(row.get("tape_time")),
        )
        pair = pairs.get(expiry)
        if pair is None:
            pair = _Pair(ticker=ticker, expiry=expiry, strike=strike)
            pairs[expiry] = pair
        elif not math.isclose(pair.strike, strike):
            # One strike per expiry is the live shape; a mismatch cannot form a straddle.
            malformed += 1
            continue
        if side == "call":
            pair.call = leg
        else:
            pair.put = leg
        pair.stock_price = pair.stock_price or _num(row.get("stock_price"))
        pair.trade_date = pair.trade_date or _day(row.get("date"))
    return pairs, malformed


def _upsert_atm(session: Session, pair: _Pair, *, fetched_at: datetime) -> None:
    row = session.get(AlfaAtm, (pair.ticker, pair.expiry))
    if row is None:
        row = AlfaAtm(ticker=pair.ticker, expiry=pair.expiry)
        session.add(row)
    row.strike = pair.strike
    row.stock_price = pair.stock_price
    row.trade_date = pair.trade_date
    row.fetched_at = fetched_at
    call, put = pair.call, pair.put
    row.call_symbol = call.symbol if call else None
    row.call_bid = call.bid if call else None
    row.call_ask = call.ask if call else None
    row.call_iv = call.iv if call else None
    row.call_tape_time = call.tape_time if call else None
    row.put_symbol = put.symbol if put else None
    row.put_bid = put.bid if put else None
    row.put_ask = put.ask if put else None
    row.put_iv = put.iv if put else None
    row.put_tape_time = put.tape_time if put else None


def _parse_osi(raw: object) -> tuple[date, Literal["call", "put"], float] | None:
    if not isinstance(raw, str):
        return None
    match = _OSI.match(raw.strip().upper())
    if match is None:
        return None
    ymd = match.group("ymd")
    try:
        expiry = date(_OSI_CENTURY + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    side: Literal["call", "put"] = "call" if match.group("cp") == "C" else "put"
    return expiry, side, int(match.group("strike")) / _OSI_STRIKE_SCALE


def _rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _num(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError):
        return None
    return value if math.isfinite(value) else None


def _int(raw: object) -> int | None:
    value = _num(raw)
    if value is None or not value.is_integer():
        return None
    return int(value)


def _day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _unique_upper(tickers: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for ticker in tickers:
        cleaned = ticker.strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)
