"""Board-side catalyst reader for the Alfa Board (Phase 5.2.B4a, contract §6 B4; decision P13).

The Phase 3.9 provider that feeds M22/M24
(``src/uoa_detector/sources/unusual_whales/providers/catalyst_calendar.py``) is NOT
used or changed. The probe found that it drops FOMC (every economic-calendar row is
typed ``report``) and that its FDA query is truncated oldest first. This module
reads the same endpoints its own way, for the board chip only:

- Earnings: ``/api/earnings/{t}``. Rows are newest first and the upcoming report is
  row 0; every row dated today or later is kept. ``report_time`` maps to an ET span:
  ``premarket`` is before the 09:30 open, ``postmarket`` is after the 16:00 close,
  anything else could be either (the whole day, ``saati bilinmiyor``). A
  ``source`` of ``estimation`` is marked ``tahmini``.
- FDA: ``/api/market/fda-calendar?ticker=&target_date_min=today``. Only a precise
  ``YYYY-MM-DD`` target counts as in window. Quarter, half and other year labels
  (``2026-Q3``, ``2027-H2``, ``2026-MID``) read ``zamanı belirsiz``. An empty target
  is a past press release and is skipped.
- Macro: ``/api/market/economic-calendar``, matched BY NAME against
  ``catalyst.macro_event_names`` (the live ``type`` is always ``report``). The
  calendar covers about ``catalyst.macro_horizon_days``; past that horizon the
  macro part reads ``bilinmiyor``.

Tables (rebuildable per fetch):
- ``alfa_catalyst``, key (ticker, kind, when_key, title). Macro rows use ticker ``*``.
- ``alfa_catalyst_fetch``, key (source, ticker): the last attempt, its status and the
  last successful fetch time. The chip needs it to tell "nothing scheduled" from
  "never fetched", which must read ``bilinmiyor``.

The chip function is pure. The board-side reader loads its inputs from the database;
no render path makes a UW call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, String, delete, or_, select
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
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.settings import BoardSettings
    from webapp.board.uw_errors import JsonClient

EARNINGS_PATH: Final = "/api/earnings/{ticker}"
FDA_PATH: Final = "/api/market/fda-calendar"
ECONOMIC_CALENDAR_PATH: Final = "/api/market/economic-calendar"
MACRO_TICKER: Final = "*"

CatalystKind = Literal["earnings", "fda", "macro"]
Precision = Literal["exact", "day", "vague"]
FetchStatus = Literal["ok", "no_data", "degraded"]
PartState = Literal["var", "yok", "zamani_belirsiz", "bilinmiyor"]

_KINDS: Final[tuple[CatalystKind, ...]] = ("earnings", "fda", "macro")

# ---------------------------------------------------------------------------
# Frozen copy
# ---------------------------------------------------------------------------

CHIP_HEAD: Final = "Vade içinde katalizör"
KIND_LABELS: Final[Mapping[CatalystKind, str]] = {
    "earnings": "Kazanç",
    "fda": "FDA",
    "macro": "Makro",
}
NONE_FOUND: Final = "yok"
UNKNOWN: Final = "bilinmiyor"
VAGUE_TIMING: Final = "zamanı belirsiz"
ESTIMATED_MARK: Final = "tahmini"
EARNINGS_TIMING_LABELS: Final[Mapping[str, str]] = {
    "premarket": "açılış öncesi",
    "postmarket": "kapanış sonrası",
    "unknown": "saati bilinmiyor",
}
BEYOND_HORIZON_TEMPLATE: Final = "{date} sonrası bilinmiyor"
M22_MAY_DIFFER: Final = (
    "Bu çip M22 olay skorundan ayrı okunur; ikisi farklı sonuç verebilir."
)
# Phase 5.2.B4b: the dominant contract's expiry has already passed, so the window
# [now, expiry close] is empty. An empty window is out of scope, never "no catalyst".
EXPIRED_WINDOW: Final = f"{CHIP_HEAD}: kapsam-dışı (vade geçti)"
# The chip could not be read at all (no source, or a failed read): unknown, never "none".
CHIP_UNKNOWN: Final = f"{CHIP_HEAD}: {UNKNOWN}"
_PART_SEPARATOR: Final = " · "
_EVENT_SEPARATOR: Final = ", "

_ET: Final = ZoneInfo("America/New_York")
# Market structure, not cutoffs: the regular session is 09:30-16:00 ET.
_SESSION_OPEN_ET: Final = time(9, 30)
_SESSION_CLOSE_ET: Final = time(16, 0)
_MIDNIGHT: Final = time(0, 0)
_MONTHS_PER_QUARTER: Final = 3
_MONTHS_PER_HALF: Final = 6
_MONTHS_PER_YEAR: Final = 12
_ESTIMATION_SOURCE: Final = "estimation"
_KNOWN_REPORT_TIMES: Final[frozenset[str]] = frozenset({"premarket", "postmarket"})
_TARGET_DAY: Final = re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$")
_TARGET_QUARTER: Final = re.compile(r"^(?P<y>\d{4})-Q(?P<n>[1-4])$")
_TARGET_HALF: Final = re.compile(r"^(?P<y>\d{4})-H(?P<n>[12])$")
_TARGET_YEAR_LABEL: Final = re.compile(r"^(?P<y>\d{4})-[A-Z]+$")
_DEGRADED: Final = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class AlfaCatalyst(AlfaBase):
    """A catalyst with its possible time span in UTC (rebuildable per fetch)."""

    __tablename__ = "alfa_catalyst"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    when_key: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, primary_key=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    precision: Mapped[str] = mapped_column(String)
    timing: Mapped[str | None] = mapped_column(String, nullable=True)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlfaCatalystFetch(AlfaBase):
    """Fetch coverage per source and ticker, so "never fetched" is not read as "none"."""

    __tablename__ = "alfa_catalyst_fetch"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def ensure_catalyst_tables(engine: Engine) -> None:
    for model in (AlfaCatalyst, AlfaCatalystFetch):
        cast("Table", model.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Reports and views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalystRefreshReport:
    fetched_at: datetime
    stored: tuple[tuple[str, str, int], ...]  # (source, ticker, rows)
    no_data: tuple[tuple[str, str], ...]
    degraded: tuple[tuple[str, str], ...]
    requests: int


@dataclass(frozen=True)
class CatalystView:
    ticker: str
    kind: CatalystKind
    when_key: str
    title: str
    starts_at: datetime
    ends_at: datetime
    precision: Precision
    timing: str | None
    estimated: bool
    detail: str | None
    fetched_at: datetime


@dataclass(frozen=True)
class CatalystFetchView:
    source: CatalystKind
    ticker: str
    last_attempt_at: datetime
    last_status: str
    last_success_at: datetime | None


@dataclass(frozen=True)
class CatalystPart:
    kind: CatalystKind
    state: PartState
    events: tuple[CatalystView, ...]
    vague_events: tuple[CatalystView, ...]
    beyond_horizon: bool
    text: str


@dataclass(frozen=True)
class CatalystChip:
    ticker: str
    window_start: datetime
    window_end: datetime
    parts: tuple[CatalystPart, ...]
    in_window: bool
    text: str


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def refresh_catalysts(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    tickers: Sequence[str],
    settings: BoardSettings,
    now: datetime,
) -> CatalystRefreshReport:
    """Daily job: earnings and FDA per ticker, the economic calendar once (2N + 1 requests)."""
    reports = [
        await refresh_earnings(client, sessions, tickers=tickers, settings=settings, now=now),
        await refresh_fda(client, sessions, tickers=tickers, settings=settings, now=now),
        await refresh_macro(client, sessions, settings=settings, now=now),
    ]
    return CatalystRefreshReport(
        fetched_at=_as_utc(now),
        stored=tuple(x for r in reports for x in r.stored),
        no_data=tuple(x for r in reports for x in r.no_data),
        degraded=tuple(x for r in reports for x in r.degraded),
        requests=sum(r.requests for r in reports),
    )


async def refresh_earnings(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    tickers: Sequence[str],
    settings: BoardSettings,
    now: datetime,
) -> CatalystRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    today = fetched_at.astimezone(_ET).date()
    acc = _Accumulator()
    for ticker in _unique_upper(tickers):
        status, resp = await _fetch(client, EARNINGS_PATH.format(ticker=ticker), None)
        acc.requests += 1
        events = _earnings_events(resp, ticker=ticker, today=today) if resp is not None else []
        _store(sessions, source="earnings", ticker=ticker, events=events, status=status,
               fetched_at=fetched_at)
        acc.add("earnings", ticker, status, len(events))
    return acc.report(fetched_at)


async def refresh_fda(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    tickers: Sequence[str],
    settings: BoardSettings,
    now: datetime,
) -> CatalystRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    today = fetched_at.astimezone(_ET).date()
    acc = _Accumulator()
    for ticker in _unique_upper(tickers):
        status, resp = await _fetch(
            client, FDA_PATH, {"ticker": ticker, "target_date_min": today.isoformat()},
        )
        acc.requests += 1
        events = _fda_events(resp, ticker=ticker, today=today) if resp is not None else []
        _store(sessions, source="fda", ticker=ticker, events=events, status=status,
               fetched_at=fetched_at)
        acc.add("fda", ticker, status, len(events))
    return acc.report(fetched_at)


async def refresh_macro(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    settings: BoardSettings,
    now: datetime,
) -> CatalystRefreshReport:
    fetched_at = _as_utc(now)
    acc = _Accumulator()
    status, resp = await _fetch(client, ECONOMIC_CALENDAR_PATH, None)
    acc.requests += 1
    events = (
        _macro_events(resp, names=settings.catalyst.macro_event_names)
        if resp is not None else []
    )
    _store(sessions, source="macro", ticker=MACRO_TICKER, events=events, status=status,
           fetched_at=fetched_at)
    acc.add("macro", MACRO_TICKER, status, len(events))
    return acc.report(fetched_at)


# ---------------------------------------------------------------------------
# Reader and chip
# ---------------------------------------------------------------------------


def load_catalyst_inputs(
    session: Session, ticker: str,
) -> tuple[tuple[CatalystView, ...], tuple[CatalystFetchView, ...]]:
    """Stored events and fetch coverage for ``ticker`` plus the market-wide macro rows."""
    symbol = ticker.strip().upper()
    events = session.scalars(
        select(AlfaCatalyst)
        .where(or_(AlfaCatalyst.ticker == symbol, AlfaCatalyst.ticker == MACRO_TICKER))
        .order_by(AlfaCatalyst.starts_at, AlfaCatalyst.kind, AlfaCatalyst.title),
    ).all()
    fetches = session.scalars(
        select(AlfaCatalystFetch).where(
            or_(AlfaCatalystFetch.ticker == symbol, AlfaCatalystFetch.ticker == MACRO_TICKER),
        ),
    ).all()
    return (
        tuple(_event_view(e) for e in events if e.kind in _KINDS),
        tuple(_fetch_view(f) for f in fetches if f.source in _KINDS),
    )


def load_catalyst_inputs_many(
    session: Session, tickers: Sequence[str],
) -> dict[str, tuple[tuple[CatalystView, ...], tuple[CatalystFetchView, ...]]]:
    """Stored events and fetch coverage for many tickers, in TWO queries (Phase 5.2.B-fix7).

    The render path asks for one chip per board row, so loading each ticker on
    its own was two round trips per ticker (review RB-02). The market-wide macro
    rows are read once and handed to every ticker, exactly as the single-ticker
    loader does.
    """
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols:
        return {}
    lookup = {*symbols, MACRO_TICKER}
    events = session.scalars(
        select(AlfaCatalyst)
        .where(AlfaCatalyst.ticker.in_(lookup))
        .order_by(AlfaCatalyst.starts_at, AlfaCatalyst.kind, AlfaCatalyst.title),
    ).all()
    fetches = session.scalars(
        select(AlfaCatalystFetch).where(AlfaCatalystFetch.ticker.in_(lookup)),
    ).all()
    views = [_event_view(e) for e in events if e.kind in _KINDS]
    fetch_views = [_fetch_view(f) for f in fetches if f.source in _KINDS]
    return {
        symbol: (
            tuple(v for v in views if v.ticker in (symbol, MACRO_TICKER)),
            tuple(f for f in fetch_views if f.ticker in (symbol, MACRO_TICKER)),
        )
        for symbol in dict.fromkeys(symbols)
    }


def read_catalyst_chip(
    session: Session,
    *,
    ticker: str,
    window_start: datetime,
    window_end: datetime,
    settings: BoardSettings,
) -> CatalystChip:
    events, fetches = load_catalyst_inputs(session, ticker)
    return catalyst_chip(
        events, fetches, ticker=ticker, window_start=window_start, window_end=window_end,
        settings=settings,
    )


# The engine whose catalyst tables are known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_board_catalysts(
    engine: Engine,
    windows: Sequence[tuple[str, datetime]],
    *,
    settings: BoardSettings,
    now: datetime,
) -> dict[tuple[str, datetime], CatalystChip]:
    """One chip per (ticker, window end) for the render path: database reads only.

    Stored events and fetch coverage are loaded once per ticker, however many
    rows share it. The tables are created once per engine, so a fresh database
    reads ``bilinmiyor`` instead of failing.
    """
    wanted = [
        (ticker.strip().upper(), _as_utc(end)) for ticker, end in windows if ticker.strip()
    ]
    if not wanted:
        return {}
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_catalyst_tables(engine)
        _tables_ready_for[:] = [engine]
    out: dict[tuple[str, datetime], CatalystChip] = {}
    with Session(engine) as session:
        loaded = load_catalyst_inputs_many(session, [ticker for ticker, _end in wanted])
    for ticker, end in dict.fromkeys(wanted):
        events, fetches = loaded.get(ticker, ((), ()))
        out[(ticker, end)] = catalyst_chip(
            events, fetches, ticker=ticker, window_start=now, window_end=end,
            settings=settings,
        )
    return out


def catalyst_chip(
    events: Sequence[CatalystView],
    fetches: Sequence[CatalystFetchView],
    *,
    ticker: str,
    window_start: datetime,
    window_end: datetime,
    settings: BoardSettings,
) -> CatalystChip:
    """Catalysts between ``window_start`` and ``window_end`` (normally now and the expiry close)."""
    symbol = ticker.strip().upper()
    start, end = _as_utc(window_start), _as_utc(window_end)
    parts = tuple(
        _part(kind, events, fetches, ticker=symbol, start=start, end=end, settings=settings)
        for kind in _KINDS
    )
    text = CHIP_HEAD + ": " + _PART_SEPARATOR.join(p.text for p in parts)
    return CatalystChip(
        ticker=symbol, window_start=start, window_end=end, parts=parts,
        in_window=any(p.state == "var" for p in parts), text=text,
    )


# ---------------------------------------------------------------------------
# Private: fetch and store
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    ticker: str
    kind: CatalystKind
    when_key: str
    title: str
    starts_at: datetime
    ends_at: datetime
    precision: Precision
    timing: str | None = None
    estimated: bool = False
    detail: str | None = None


@dataclass
class _Accumulator:
    stored: list[tuple[str, str, int]] = field(default_factory=list)
    no_data: list[tuple[str, str]] = field(default_factory=list)
    degraded: list[tuple[str, str]] = field(default_factory=list)
    requests: int = 0

    def add(self, source: str, ticker: str, status: FetchStatus, rows: int) -> None:
        if status == "degraded":
            self.degraded.append((source, ticker))
        elif status == "no_data":
            self.no_data.append((source, ticker))
        else:
            self.stored.append((source, ticker, rows))

    def report(self, fetched_at: datetime) -> CatalystRefreshReport:
        return CatalystRefreshReport(
            fetched_at=fetched_at, stored=tuple(self.stored), no_data=tuple(self.no_data),
            degraded=tuple(self.degraded), requests=self.requests,
        )


async def _fetch(
    client: JsonClient, path: str, params: dict[str, Any] | None,
) -> tuple[FetchStatus, dict[str, Any] | None]:
    try:
        return "ok", await client.request_json(path, params=params)
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        return "no_data", None
    except _DEGRADED:
        return "degraded", None


def _store(
    sessions: sessionmaker[Session],
    *,
    source: CatalystKind,
    ticker: str,
    events: Sequence[_Event],
    status: FetchStatus,
    fetched_at: datetime,
) -> None:
    """Rebuild (ticker, kind) rows on success or no-data; keep the old rows when degraded."""
    with sessions() as session, session.begin():
        fetch = session.get(AlfaCatalystFetch, (source, ticker))
        if fetch is None:
            fetch = AlfaCatalystFetch(source=source, ticker=ticker)
            session.add(fetch)
        fetch.last_attempt_at = fetched_at
        fetch.last_status = status
        if status == "degraded":
            return
        fetch.last_success_at = fetched_at
        session.execute(
            delete(AlfaCatalyst).where(AlfaCatalyst.ticker == ticker, AlfaCatalyst.kind == source),
        )
        unique: dict[tuple[str, str], _Event] = {}
        for event in events:
            unique.setdefault((event.when_key, event.title), event)
        for event in unique.values():
            session.add(AlfaCatalyst(
                ticker=event.ticker, kind=event.kind, when_key=event.when_key,
                title=event.title, starts_at=event.starts_at, ends_at=event.ends_at,
                precision=event.precision, timing=event.timing, estimated=event.estimated,
                detail=event.detail, fetched_at=fetched_at,
            ))


def _earnings_events(resp: Mapping[str, Any], *, ticker: str, today: date) -> list[_Event]:
    best: dict[date, _Event] = {}
    for row in _rows(resp.get("data")):
        day = _day(row.get("report_date"))
        if day is None or day < today:
            continue
        report_time = _label(row.get("report_time"))
        timing = report_time if report_time in _KNOWN_REPORT_TIMES else "unknown"
        previous = best.get(day)
        if previous is not None and not (previous.timing == "unknown" and timing != "unknown"):
            continue
        midnight = datetime.combine(day, _MIDNIGHT, tzinfo=_ET)
        next_midnight = datetime.combine(day + timedelta(days=1), _MIDNIGHT, tzinfo=_ET)
        if timing == "premarket":
            span = (midnight, datetime.combine(day, _SESSION_OPEN_ET, tzinfo=_ET))
        elif timing == "postmarket":
            span = (datetime.combine(day, _SESSION_CLOSE_ET, tzinfo=_ET), next_midnight)
        else:
            span = (midnight, next_midnight)
        source = _label(row.get("source"))
        best[day] = _Event(
            ticker=ticker, kind="earnings", when_key=day.isoformat(), title="earnings",
            starts_at=span[0].astimezone(UTC), ends_at=span[1].astimezone(UTC),
            precision="day", timing=timing, estimated=source == _ESTIMATION_SOURCE,
            detail=source or None,
        )
    return [best[d] for d in sorted(best)]


def _fda_events(resp: Mapping[str, Any], *, ticker: str, today: date) -> list[_Event]:
    events: list[_Event] = []
    for row in _rows(resp.get("data")):
        row_ticker = str(row.get("ticker") or "").strip().upper()
        if row_ticker and row_ticker != ticker:
            continue
        target = str(row.get("target_date") or "").strip().upper()
        parsed = _target_span(target)
        if parsed is None:
            continue
        starts, ends, precision = parsed
        if precision == "day" and starts.astimezone(_ET).date() < today:
            continue
        catalyst = str(row.get("catalyst") or row.get("event_type") or "").strip()
        drug = str(row.get("drug") or "").strip()
        title = " / ".join(p for p in (catalyst, drug) if p) or "fda"
        status = str(row.get("status") or "").strip()
        events.append(_Event(
            ticker=ticker, kind="fda", when_key=target, title=title, starts_at=starts,
            ends_at=ends, precision=precision, timing=target, detail=status or None,
        ))
    return events


def _macro_events(resp: Mapping[str, Any], *, names: Sequence[str]) -> list[_Event]:
    events: list[_Event] = []
    for row in _rows(resp.get("data")):
        name = str(row.get("event") or "").strip()
        when = _ts(row.get("time"))
        if not name or when is None:
            continue
        lowered = name.lower()
        keyword = next((k for k in names if k.strip() and k.strip().lower() in lowered), None)
        if keyword is None:
            continue
        events.append(_Event(
            ticker=MACRO_TICKER, kind="macro", when_key=when.isoformat(), title=name,
            starts_at=when, ends_at=when, precision="exact", timing=keyword.strip().lower(),
            detail=str(row.get("reported_period") or "").strip() or None,
        ))
    return events


def _target_span(target: str) -> tuple[datetime, datetime, Precision] | None:
    if match := _TARGET_DAY.match(target):
        try:
            day = date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            return None
        return (
            datetime.combine(day, _MIDNIGHT, tzinfo=_ET).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), _MIDNIGHT, tzinfo=_ET).astimezone(UTC),
            "day",
        )
    if match := _TARGET_QUARTER.match(target):
        first_month = (int(match["n"]) - 1) * _MONTHS_PER_QUARTER + 1
        return (*_month_span(int(match["y"]), first_month, _MONTHS_PER_QUARTER), "vague")
    if match := _TARGET_HALF.match(target):
        first_month = (int(match["n"]) - 1) * _MONTHS_PER_HALF + 1
        return (*_month_span(int(match["y"]), first_month, _MONTHS_PER_HALF), "vague")
    if match := _TARGET_YEAR_LABEL.match(target):
        return (*_month_span(int(match["y"]), 1, _MONTHS_PER_YEAR), "vague")
    return None


def _month_span(year: int, first_month: int, months: int) -> tuple[datetime, datetime]:
    start = datetime.combine(date(year, first_month, 1), _MIDNIGHT, tzinfo=_ET)
    month_index = first_month - 1 + months
    end_year = year + month_index // _MONTHS_PER_YEAR
    end_month = month_index % _MONTHS_PER_YEAR + 1
    end = datetime.combine(date(end_year, end_month, 1), _MIDNIGHT, tzinfo=_ET)
    return start.astimezone(UTC), end.astimezone(UTC)


# ---------------------------------------------------------------------------
# Private: chip
# ---------------------------------------------------------------------------


def _part(
    kind: CatalystKind,
    events: Sequence[CatalystView],
    fetches: Sequence[CatalystFetchView],
    *,
    ticker: str,
    start: datetime,
    end: datetime,
    settings: BoardSettings,
) -> CatalystPart:
    owner = MACRO_TICKER if kind == "macro" else ticker
    label = KIND_LABELS[kind]
    fetch = next(
        (f for f in fetches if f.source == kind and f.ticker == owner and f.last_success_at),
        None,
    )
    if fetch is None or fetch.last_success_at is None:
        return CatalystPart(kind=kind, state="bilinmiyor", events=(), vague_events=(),
                            beyond_horizon=False, text=f"{label}: {UNKNOWN}")
    mine = [e for e in events if e.kind == kind and e.ticker == owner]
    hits = tuple(e for e in mine if e.precision != "vague" and _overlaps(e, start, end))
    vague = tuple(e for e in mine if e.precision == "vague" and _overlaps(e, start, end))
    beyond = False
    horizon_end = start
    if kind == "macro":
        horizon_end = fetch.last_success_at + timedelta(days=settings.catalyst.macro_horizon_days)
        beyond = end > horizon_end
    bits: list[str] = []
    if hits:
        state: PartState = "var"
        bits.append(_EVENT_SEPARATOR.join(_event_text(e) for e in hits))
    elif vague:
        state = "zamani_belirsiz"
    elif beyond:
        state = "bilinmiyor"
    else:
        state = "yok"
    if vague:
        bits.append(f"{VAGUE_TIMING} ({_EVENT_SEPARATOR.join(e.when_key for e in vague)})")
    if beyond:
        bits.append(BEYOND_HORIZON_TEMPLATE.format(date=_short_date(horizon_end)))
    if not bits:
        bits.append(NONE_FOUND)
    return CatalystPart(
        kind=kind, state=state, events=hits, vague_events=vague, beyond_horizon=beyond,
        text=f"{label}: " + _PART_SEPARATOR.join(bits),
    )


def _event_text(event: CatalystView) -> str:
    local = event.starts_at.astimezone(_ET)
    if event.kind == "macro":
        return f"{event.timing} {local:%d.%m %H:%M} ET"
    if event.kind == "earnings":
        timing = EARNINGS_TIMING_LABELS.get(event.timing or "unknown", EARNINGS_TIMING_LABELS["unknown"])
        text = f"{local:%d.%m} {timing}"
        return f"{text} ({ESTIMATED_MARK})" if event.estimated else text
    return f"{local:%d.%m}"


def _overlaps(event: CatalystView, start: datetime, end: datetime) -> bool:
    if event.starts_at == event.ends_at:
        return start <= event.starts_at < end
    return event.starts_at < end and event.ends_at > start


def _short_date(value: datetime) -> str:
    return f"{value.astimezone(_ET):%d.%m}"


def _event_view(row: AlfaCatalyst) -> CatalystView:
    return CatalystView(
        ticker=row.ticker, kind=cast("CatalystKind", row.kind), when_key=row.when_key,
        title=row.title, starts_at=_as_utc(row.starts_at), ends_at=_as_utc(row.ends_at),
        precision=cast("Precision", row.precision), timing=row.timing,
        estimated=bool(row.estimated), detail=row.detail, fetched_at=_as_utc(row.fetched_at),
    )


def _fetch_view(row: AlfaCatalystFetch) -> CatalystFetchView:
    return CatalystFetchView(
        source=cast("CatalystKind", row.source), ticker=row.ticker,
        last_attempt_at=_as_utc(row.last_attempt_at), last_status=row.last_status,
        last_success_at=_as_utc(row.last_success_at) if row.last_success_at else None,
    )


# ---------------------------------------------------------------------------
# Private: parsing
# ---------------------------------------------------------------------------


def _rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _label(raw: object) -> str:
    return raw.strip().lower() if isinstance(raw, str) else ""


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
        if cleaned and cleaned != MACRO_TICKER:
            seen.setdefault(cleaned, None)
    return list(seen)
