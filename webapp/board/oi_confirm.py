"""T+1 open-interest confirmation for the Alfa Board (Phase 5.2.B4a, contract §6 B4).

Decision P10: the Açık pozisyon evidence family comes only from this table.

Unusual Whales open interest is start-of-day (probe 2026-09-15). The historic row
dated T holds the OI left after session T-1; the row dated T+1, published
pre-market on T+1, holds the OI left after session T. A print flagged in session T
is therefore confirmed by ``ΔOI = OI(T+1) - OI(T)``:

- ``açılış (T+1 OI teyitli)``: ΔOI >= ``opening_closing.confirm_open_min_ratio`` x flagged size;
- ``kapanış (T+1 OI düştü)``: ΔOI <= ``opening_closing.confirm_close_max_ratio`` x flagged size;
- ``henüz doğrulanmadı``: T+1 not published yet (status ``bekliyor``), or ΔOI between
  the two cutoffs (final status ``arada``);
- ``kapsam-dışı (T+1'den önce vade)``: the contract expires before the next session;
- ``doğrulanamadı (T+1 verisi penceresi kapandı)``: the flag is
  ``opening_closing.max_confirm_age_sessions`` sessions old, so its session has left
  the ``historic`` window the request returns. It can never be resolved, so it is
  retired once (final status ``kacirildi``, read as ``bilinmiyor``) instead of
  costing one request every pre-market until the contract expires (review RB-01).

``alfa_oi_confirm`` is append-only forward evidence (contract §4.2). A row is
inserted once per (option_symbol, trade_date) and never deleted or reset. Its
status advances from ``bekliyor`` to a final status once, through an UPDATE guarded
on ``status = 'bekliyor'``; the cutoffs and board-profile hash used are recorded
with it.

The next session is the next weekday. Market holidays are not modelled, the same as
``uoa_detector.sources.market_hours``. A pending row is only queried while its
contract has not expired, so a holiday-shifted T+1 that never arrives stays
``henüz doğrulanmadı`` instead of burning requests.

UW errors: NotFound is no data; rate limit, transient and an open breaker mark the
contract degraded and the job continues; daily limit and auth errors propagate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from weakref import WeakSet
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, Float, Integer, String, select, update
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
    from sqlalchemy.engine import CursorResult, Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.evidence import OIConfirmState
    from webapp.board.settings import BoardSettings
    from webapp.board.uw_errors import JsonClient

HISTORIC_PATH: Final = "/api/option-contract/{symbol}/historic"
# Request window from contract §4.4 (`historic?limit=5`): enough rows to hold T and T+1
# when the job runs within a few sessions of the flag. Not a cutoff.
HISTORIC_LIMIT: Final = 5

OiStatus = Literal["bekliyor", "acilis", "kapanis", "arada", "kapsam_disi", "kacirildi"]
EvidenceState = Literal["lehte", "aleyhte", "bilinmiyor", "kapsam-dışı"]

# Frozen copy (contract §6 B4, byte for byte).
STATUS_LABELS: Final[Mapping[OiStatus, str]] = {
    "bekliyor": "henüz doğrulanmadı",
    "arada": "henüz doğrulanmadı",
    "acilis": "açılış (T+1 OI teyitli)",
    "kapanis": "kapanış (T+1 OI düştü)",
    "kapsam_disi": "kapsam-dışı (T+1'den önce vade)",
    # Phase 5.2.B-fix6 (review RB-01): the historic window no longer contains the print's
    # session, so this flag can never be resolved. It is a permanent unknown, not a "not yet".
    "kacirildi": "doğrulanamadı (T+1 verisi penceresi kapandı)",
}
NO_ROW_LABEL: Final = "henüz doğrulanmadı"

# Contract §9, Açık pozisyon row.
_EVIDENCE: Final[Mapping[OiStatus, EvidenceState]] = {
    "acilis": "lehte",
    "kapanis": "aleyhte",
    "bekliyor": "bilinmiyor",
    "arada": "bilinmiyor",
    "kapsam_disi": "kapsam-dışı",
    "kacirildi": "bilinmiyor",
}
_FINAL: Final[frozenset[str]] = frozenset(
    {"acilis", "kapanis", "arada", "kapsam_disi", "kacirildi"},
)
_PENDING: Final = "bekliyor"
_MISSED: Final = "kacirildi"

_ET: Final = ZoneInfo("America/New_York")
_SATURDAY: Final = 5
_DEGRADED: Final = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)
_OSI: Final = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9.]*?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$",
)
_OSI_STRIKE_SCALE: Final = 1000
_OSI_CENTURY: Final = 2000


class AlfaOiConfirm(AlfaBase):
    """One flagged contract per session, confirmed against next-day open interest (append-only)."""

    __tablename__ = "alfa_oi_confirm"

    option_symbol: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    option_type: Mapped[str] = mapped_column(String)
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[date] = mapped_column(Date)
    flagged_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oi_t: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oi_t1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    t1_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delta_oi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_min_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_max_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    board_profile_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def ensure_oi_confirm_tables(engine: Engine) -> None:
    """Create ``alfa_oi_confirm`` if missing. Append-only: never dropped or reset."""
    cast("Table", AlfaOiConfirm.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class FlaggedContract:
    """A dominant contract flagged in session ``trade_date`` (ET), with its flagged size.

    The caller aggregates a session's prints first; a repeated (symbol, trade_date)
    keeps the first recorded size.
    """

    option_symbol: str
    ticker: str
    trade_date: date
    flagged_size: int


@dataclass(frozen=True)
class OiConfirmReport:
    checked_at: datetime
    recorded: int
    already_recorded: int
    out_of_scope: int
    rejected: tuple[str, ...]
    resolved: tuple[tuple[str, date, OiStatus], ...]
    awaiting: tuple[tuple[str, date], ...]
    no_data: tuple[str, ...]
    degraded: tuple[str, ...]
    requests: int
    # Phase 5.2.B-fix6: rows retired because their session left the historic window.
    retired: tuple[tuple[str, date], ...] = ()


@dataclass(frozen=True)
class OiConfirmView:
    option_symbol: str
    trade_date: date
    ticker: str
    option_type: str
    strike: float
    expiry: date
    flagged_size: int
    status: OiStatus
    label: str
    evidence_state: EvidenceState
    final: bool
    oi_t: int | None
    oi_t1: int | None
    t1_date: date | None
    delta_oi: int | None
    delta_ratio: float | None
    resolved_at: datetime | None


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def confirm_open_interest(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    flagged: Sequence[FlaggedContract],
    settings: BoardSettings,
    now: datetime,
) -> OiConfirmReport:
    """Daily pre-market job: record the previous session's flags, then resolve pending rows."""
    checked_at = _as_utc(now)
    today = checked_at.astimezone(_ET).date()
    profile_hash = settings.content_hash()
    recorded = already = out_of_scope = 0
    rejected: list[str] = []
    with sessions() as session, session.begin():
        for item in flagged:
            parsed = _parse_osi(item.option_symbol)
            if (
                parsed is None or item.flagged_size <= 0 or not item.ticker.strip()
                or item.trade_date.weekday() >= _SATURDAY
            ):
                rejected.append(item.option_symbol)
                continue
            symbol = item.option_symbol.strip().upper()
            if session.get(AlfaOiConfirm, (symbol, item.trade_date)) is not None:
                already += 1
                continue
            expiry, option_type, strike = parsed
            beyond = expiry < next_session(item.trade_date)
            session.add(AlfaOiConfirm(
                option_symbol=symbol, trade_date=item.trade_date,
                ticker=item.ticker.strip().upper(), option_type=option_type, strike=strike,
                expiry=expiry, flagged_size=item.flagged_size,
                status="kapsam_disi" if beyond else _PENDING, created_at=checked_at,
                board_profile_hash=profile_hash if beyond else None,
                resolved_at=checked_at if beyond else None,
            ))
            session.flush()
            recorded += 1
            out_of_scope += int(beyond)

    # A flag whose session has left the historic window can never be resolved: retire it
    # instead of spending one request on it every pre-market (review RB-01).
    retired: list[tuple[str, date]] = []
    max_age_sessions = settings.opening_closing.max_confirm_age_sessions
    with sessions() as session, session.begin():
        stale = session.scalars(
            select(AlfaOiConfirm)
            .where(
                AlfaOiConfirm.status == _PENDING,
                AlfaOiConfirm.trade_date < today,
                AlfaOiConfirm.expiry >= today,
            )
            .order_by(AlfaOiConfirm.option_symbol, AlfaOiConfirm.trade_date),
        ).all()
        for row in stale:
            if sessions_since(row.trade_date, today) < max_age_sessions:
                continue
            row.status = _MISSED
            row.board_profile_hash = profile_hash
            row.resolved_at = checked_at
            retired.append((row.option_symbol, row.trade_date))

    with sessions() as session:
        pending = session.scalars(
            select(AlfaOiConfirm)
            .where(
                AlfaOiConfirm.status == _PENDING,
                AlfaOiConfirm.trade_date < today,
                AlfaOiConfirm.expiry >= today,
            )
            .order_by(AlfaOiConfirm.option_symbol, AlfaOiConfirm.trade_date),
        ).all()
        grouped: dict[str, list[tuple[date, int]]] = {}
        for row in pending:
            if next_session(row.trade_date) <= today:
                grouped.setdefault(row.option_symbol, []).append((row.trade_date, row.flagged_size))

    resolved: list[tuple[str, date, OiStatus]] = []
    awaiting: list[tuple[str, date]] = []
    no_data: list[str] = []
    degraded: list[str] = []
    requests = 0
    ratios = settings.opening_closing
    for symbol, items in grouped.items():
        requests += 1
        try:
            resp = await client.request_json(
                HISTORIC_PATH.format(symbol=symbol), params={"limit": HISTORIC_LIMIT},
            )
        except UnusualWhalesDailyLimitError:
            raise
        except UnusualWhalesNotFoundError:
            no_data.append(symbol)
            continue
        except _DEGRADED:
            degraded.append(symbol)
            continue
        oi_by_day = _parse_chains(resp.get("chains"))
        for trade_date, size in items:
            oi_t = oi_by_day.get(trade_date)
            later = sorted(d for d in oi_by_day if d > trade_date)
            if oi_t is None or not later:
                awaiting.append((symbol, trade_date))
                continue
            t1_date = later[0]
            oi_t1 = oi_by_day[t1_date]
            delta = oi_t1 - oi_t
            status = classify_delta(delta, size, settings=settings)
            with sessions() as session, session.begin():
                result = cast("CursorResult[Any]", session.execute(
                    update(AlfaOiConfirm)
                    .where(
                        AlfaOiConfirm.option_symbol == symbol,
                        AlfaOiConfirm.trade_date == trade_date,
                        AlfaOiConfirm.status == _PENDING,
                    )
                    .values(
                        status=status, oi_t=oi_t, oi_t1=oi_t1, t1_date=t1_date, delta_oi=delta,
                        open_min_ratio=ratios.confirm_open_min_ratio,
                        close_max_ratio=ratios.confirm_close_max_ratio,
                        board_profile_hash=profile_hash, resolved_at=checked_at,
                    ),
                ))
            if result.rowcount == 1:
                resolved.append((symbol, trade_date, status))
    return OiConfirmReport(
        checked_at=checked_at, recorded=recorded, already_recorded=already,
        out_of_scope=out_of_scope, rejected=tuple(rejected), resolved=tuple(resolved),
        awaiting=tuple(awaiting), no_data=tuple(no_data), degraded=tuple(degraded),
        requests=requests, retired=tuple(retired),
    )


# ---------------------------------------------------------------------------
# Pure logic and readers
# ---------------------------------------------------------------------------


def next_session(day: date) -> date:
    """The next weekday after ``day`` (holidays are not modelled)."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= _SATURDAY:
        nxt += timedelta(days=1)
    return nxt


def sessions_since(day: date, today: date) -> int:
    """Weekday sessions strictly after ``day``, up to and including ``today``.

    Holidays are not modelled, the same as ``next_session``. The T+1 job asks for
    ``HISTORIC_LIMIT`` rows, so a flag this many sessions old has left the window
    the request returns (Phase 5.2.B-fix6).
    """
    if today <= day:
        return 0
    count = 0
    cursor = day + timedelta(days=1)
    while cursor <= today:
        if cursor.weekday() < _SATURDAY:
            count += 1
        cursor += timedelta(days=1)
    return count


def expires_before_next_session(expiry: date, trade_date: date) -> bool:
    """True when no T+1 open-interest row can exist for the contract (kapsam-dışı)."""
    return expiry < next_session(trade_date)


def classify_delta(
    delta_oi: int, flagged_size: int, *, settings: BoardSettings,
) -> Literal["acilis", "kapanis", "arada"]:
    if flagged_size <= 0:
        return "arada"
    cutoffs = settings.opening_closing
    if delta_oi >= cutoffs.confirm_open_min_ratio * flagged_size:
        return "acilis"
    if delta_oi <= cutoffs.confirm_close_max_ratio * flagged_size:
        return "kapanis"
    return "arada"


def oi_confirm_view(row: AlfaOiConfirm) -> OiConfirmView:
    status = _status(row.status)
    ratio = (
        row.delta_oi / row.flagged_size
        if row.delta_oi is not None and row.flagged_size > 0 else None
    )
    return OiConfirmView(
        option_symbol=row.option_symbol, trade_date=row.trade_date, ticker=row.ticker,
        option_type=row.option_type, strike=row.strike, expiry=row.expiry,
        flagged_size=row.flagged_size, status=status, label=STATUS_LABELS[status],
        evidence_state=_EVIDENCE[status], final=status in _FINAL, oi_t=row.oi_t,
        oi_t1=row.oi_t1, t1_date=row.t1_date, delta_oi=row.delta_oi, delta_ratio=ratio,
        resolved_at=_as_utc(row.resolved_at) if row.resolved_at is not None else None,
    )


def load_oi_confirm(session: Session, option_symbol: str, trade_date: date) -> OiConfirmView | None:
    row = session.get(AlfaOiConfirm, (option_symbol.strip().upper(), trade_date))
    return oi_confirm_view(row) if row is not None else None


def load_oi_confirms(
    session: Session, keys: Iterable[tuple[str, date]],
) -> dict[tuple[str, date], OiConfirmView]:
    """Every stored confirmation among ``keys``, in ONE query (Phase 5.2.B-fix7).

    The render path asks for one key per board row, so a ``session.get`` per key
    was one round trip per row (review RB-02). The symbols and the trade dates
    are filtered in SQL and the exact pairs in Python, which keeps the statement
    dialect-neutral.
    """
    wanted = {(symbol.strip().upper(), trade_date) for symbol, trade_date in keys}
    if not wanted:
        return {}
    rows = session.scalars(
        select(AlfaOiConfirm).where(
            AlfaOiConfirm.option_symbol.in_({symbol for symbol, _day in wanted}),
            AlfaOiConfirm.trade_date.in_({day for _symbol, day in wanted}),
        ),
    ).all()
    return {
        (row.option_symbol, row.trade_date): oi_confirm_view(row)
        for row in rows
        if (row.option_symbol, row.trade_date) in wanted
    }


def oi_label(view: OiConfirmView | None) -> str:
    """UI label; a contract with no confirmation row reads ``henüz doğrulanmadı``."""
    return NO_ROW_LABEL if view is None else view.label


def oi_evidence_state(view: OiConfirmView | None) -> EvidenceState:
    """Açık pozisyon family state (contract §9); no row is ``bilinmiyor``."""
    return "bilinmiyor" if view is None else view.evidence_state


# Phase 5.2.B4b: the stored status as the evidence layer's four-state reading
# (``webapp/board/evidence.py``). ``arada`` and ``bekliyor`` both read "not confirmed yet".
BOARD_STATES: Final[Mapping[OiStatus, OIConfirmState]] = {
    "acilis": "opening",
    "kapanis": "closing",
    "bekliyor": "unconfirmed",
    "arada": "unconfirmed",
    "kapsam_disi": "expires_before_t1",
    # Phase 5.2.B-fix6: a retired flag is a permanent unknown, so the Açık pozisyon
    # family reads it exactly like an unconfirmed one (contract §9).
    "kacirildi": "unconfirmed",
}


def board_state(view: OiConfirmView | None) -> OIConfirmState | None:
    """What the Açık pozisyon family reads; ``None`` (no row) is ``bilinmiyor (T+1 bekleniyor)``."""
    return None if view is None else BOARD_STATES[view.status]


# The engines whose confirmation table is known to exist. A set, not one slot: the web app
# and the refresher hold different Engine objects for the same database and alternate
# through this reader, which made the one-slot guard re-issue catalog DDL on every call
# (review RB-05).
_tables_ready_for: WeakSet[Engine] = WeakSet()


def read_board_oi(
    engine: Engine, keys: Iterable[tuple[str, date]],
) -> dict[tuple[str, date], OiConfirmView]:
    """Stored confirmations for the given (option symbol, trade date) keys. Reads only.

    Creates the table once per engine, so a fresh database renders
    ``henüz doğrulanmadı`` instead of failing.
    """
    wanted = [(symbol.strip().upper(), day) for symbol, day in keys if symbol.strip()]
    if not wanted:
        return {}
    if engine not in _tables_ready_for:
        ensure_oi_confirm_tables(engine)
        _tables_ready_for.add(engine)
    with Session(engine) as session:
        return load_oi_confirms(session, wanted)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _status(raw: str) -> OiStatus:
    for status in STATUS_LABELS:
        if raw == status:
            return status
    msg = f"unknown alfa_oi_confirm status {raw!r}"
    raise ValueError(msg)


def _parse_chains(raw: object) -> dict[date, int]:
    out: dict[date, int] = {}
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        day = _day(row.get("date"))
        oi = _int(row.get("open_interest"))
        if day is None or oi is None or oi < 0:
            continue
        out.setdefault(day, oi)
    return out


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


def _int(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError):
        return None
    if not math.isfinite(value) or not value.is_integer():
        return None
    return int(value)


def _day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
