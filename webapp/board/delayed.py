"""Alfa Board delayed evidence families (Phase 5.2 FAZ D, data layer).

Contract ``docs/phase-5.2-alfa-board-acceptance.md`` §7, decision P14
(``docs/alfa-board-decisions.md``).

Delayed families render under ``ek kanıt (gecikmeli)`` with their filing (or
as-of) date, the delay and the outcome. They are NEVER counted: they enter no
evidence count, strength label, clean-candidate rule or counter-argument
priority. The view types below therefore carry no count field.

Table ``alfa_delayed``: key (ticker, family, dedupe_key), append-only. The
post-close daily job appends rows that are not stored yet and never rewrites,
deletes or resets one.

Families:
- ``congress`` (D1): ``GET /api/congress/recent-trades?ticker=&limit=200``.
  - The owner-named ``/api/congress/unusual-trades`` and ``/by-tickers``
    return 422 "premium endpoint" on this key (probe 2026-09-15). They are
    never called.
  - Rows arrive sorted by transaction date; records are ordered by filing date.
  - Rows carry no id. The dedupe key is a digest of politician id, ticker,
    transaction date, transaction type, amount range, filing date and issuer.
  - delay = filed_at_date - transaction_date. Above
    ``delayed.congress_late_days`` the row is marked ``geç bildirim``.
  - ``member_type`` ``executive`` (government-ethics filings, not Congress) is
    flagged separately.
  - Only buy and sell transactions are stored. The lookback window
    (``delayed.congress_lookback_days``) applies to the filing date.
- ``insider`` (D2): ``GET /api/insider/transactions?ticker_symbol=
  &form_types[]=4&form_types[]=4/A&start_date=<today - delayed.insider_lookback_days>``.
  - ``/api/insider/{t}`` is only a roster, and ``/api/market/insider-buy-sells``
    is market-wide and filing-dated. Neither is used per ticker.
  - Only transaction codes P (open-market purchase) and S (sale) are stored.
    A, F, G and J (grants, tax withholding, gifts, other) are not decisions to
    trade. Form 144 (a notice of intent) is not requested and never stored.
  - Side comes from the amount sign together with the code; when they disagree
    the side is unknown. Size = |amount| shares x price, in USD.
  - ``is_10b5_1`` (a pre-scheduled plan) is flagged; Form 4/A is flagged as an
    amendment.
  - delay = filing_date - transaction_date. The window applies to the
    transaction date, like the request's ``start_date``.
  - UW merges rows of the same person, day and code and lists their ``ids``.
    The dedupe key is the sorted ids. A stored group whose ids are a strict
    subset of another stored group's ids (a later regrouping) is not shown.
  - ``has_more`` true is reported as truncated.
- ``short_interest`` (D3): ``GET /api/shorts/{ticker}/interest-float/v2``.
  - The v1 ``/interest-float`` data ends in 2021 with impossible values. It is
    never called.
  - ``si_float`` is a fraction, displayed as a percent. ``market_date`` (the
    FINRA settlement date) is the as-of date and the dedupe key.
  - ``short_shares_available`` (10,000,000 in every probed row) is ignored and
    not stored.
  - Window: ``delayed.short_interest_lookback_days`` on the as-of date.
- ``ftd`` (D3): ``GET /api/shorts/{ticker}/ftds``.
  - ``date`` is the fail date and the dedupe key; ``quantity`` is shares;
    notional = quantity x price, in USD.
  - Only days with fails are published: a missing day means no reported fails.
  - Window: ``delayed.ftd_lookback_days`` on the fail date (a trailing window).
- Neither shorts source has a filing date. Their delay is today - the as-of (or
  fail) date, computed at render and labelled that way; ``delay_days`` is NULL.

UW error policy: NotFound -> no data; RateLimit, Transient, CircuitBreakerOpen ->
that family is degraded and the job continues; DailyLimit and Auth propagate.

Outcome: the underlying's move from the close on or before the filing (or
as-of) date to the newest close on or before ``today``, from
``alfa_daily_close``. With no session in between, or no close, it reads
``bilinmiyor``. It is never direction-signed and never scored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, cast, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Table, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.daily_close import ClosePoint, FetchStatus, load_closes, pct_move_between
from webapp.board.db import AlfaBase, session_factory
from webapp.board.honesty import ensure_clean
from webapp.board.settings import DelayedSettings
from webapp.board.uw_errors import JsonClient

_logger = logging.getLogger(__name__)

DelayedFamily = Literal["congress", "insider", "short_interest", "ftd"]
TradeSide = Literal["buy", "sell"]

_FAMILY_ORDER: Final[tuple[DelayedFamily, ...]] = get_args(DelayedFamily)

CONGRESS_RECENT_TRADES_PATH: Final = "/api/congress/recent-trades"
# Request parameter, not a cutoff: the endpoint's documented maximum page size.
# The endpoint has no paging, so a full page is reported as truncated.
_CONGRESS_LIMIT: Final = 200

INSIDER_TRANSACTIONS_PATH: Final = "/api/insider/transactions"
INSIDER_FORM_TYPES: Final[tuple[str, ...]] = ("4", "4/A")
_INSIDER_CODE_SIDE: Final[Mapping[str, TradeSide]] = MappingProxyType({"P": "buy", "S": "sell"})
_AMENDED_FORM: Final = "4/A"

SHORT_INTEREST_PATH: Final = "/api/shorts/{ticker}/interest-float/v2"
FTDS_PATH: Final = "/api/shorts/{ticker}/ftds"
_IGNORED_SHORT_INTEREST_FIELDS: Final = frozenset({"short_shares_available"})

_ET: Final = ZoneInfo("America/New_York")

# How each family's date, delay and outcome are labelled.
_DATE_KIND: Final[Mapping[str, str]] = MappingProxyType({
    "congress": "filed", "insider": "filed", "short_interest": "asof", "ftd": "fail",
})

# Frozen Turkish copy (R-WD1: every generated string passes ensure_clean).
_TEXT: Final[Mapping[str, str]] = MappingProxyType({
    "bucket": "ek kanıt (gecikmeli)",
    "exclusion": "gecikmeli veri: kanıt sayımına, güç etiketine ve karşı argümana girmez",
    "family.congress": "Kongre",
    "family.insider": "İçeriden",
    "family.short_interest": "Short",
    "family.ftd": "FTD",
    "date.filed": "bildirim tarihi",
    "date.asof": "itibarıyla tarihi",
    "date.fail": "FTD günü",
    "delay.filed": "işlemden {days} gün sonra bildirildi",
    "delay.asof": "bugün - itibarıyla tarihi = {days} gün (kaynakta bildirim tarihi yok)",
    "delay.fail": "bugün - FTD günü = {days} gün (kaynakta bildirim tarihi yok)",
    "side.buy": "alış",
    "side.sell": "satış",
    "size.shares": "{shares} hisse x ${price} ≈ ${usd}",
    "size.short_interest": "açığa satış / serbest dolaşım %{pct}; short kapatma süresi {dtc} gün",
    "size.short_interest_no_dtc": "açığa satış / serbest dolaşım %{pct}",
    "flag.late": "geç bildirim",
    "flag.executive": "yürütme beyanı (Kongre üyesi değil)",
    "flag.10b5_1": "10b5-1 planlı işlem",
    "flag.amended": "düzeltilmiş beyan (Form 4/A)",
    "outcome.filed": "bildirim tarihinden beri dayanak %{pct} ({base} → {through} kapanış)",
    "outcome.asof": "itibarıyla tarihinden beri dayanak %{pct} ({base} → {through} kapanış)",
    "outcome.fail": "FTD gününden beri dayanak %{pct} ({base} → {through} kapanış)",
    "outcome.unknown": "bilinmiyor",
})


def _say(key: str, **values: object) -> str:
    return ensure_clean(_TEXT[key].format(**values))


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


class AlfaDelayed(AlfaBase):
    """One delayed disclosure per (ticker, family, dedupe_key). Append-only."""

    __tablename__ = "alfa_delayed"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    family: Mapped[str] = mapped_column(String, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String, primary_key=True)
    filed_or_asof_date: Mapped[date] = mapped_column(Date)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # filed - transaction, in days. NULL for as-of families, whose delay grows with today.
    delay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side: Mapped[str | None] = mapped_column(String, nullable=True)  # buy | sell
    size_text: Mapped[str | None] = mapped_column(String, nullable=True)  # as filed
    size_low: Mapped[float | None] = mapped_column(Float, nullable=True)  # USD
    size_high: Mapped[float | None] = mapped_column(Float, nullable=True)  # USD
    # Flags are NULL where they do not apply to the family.
    flag_late: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_executive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_10b5_1: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    form: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)  # the UW row as received
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_delayed_tables(engine: Engine) -> None:
    """Create ``alfa_delayed`` when missing. Never alters or drops anything."""
    cast(Table, AlfaDelayed.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class DelayedRecord:
    """A parsed delayed row, free of ORM state."""

    ticker: str
    family: DelayedFamily
    dedupe_key: str
    filed_or_asof_date: date
    transaction_date: date | None
    delay_days: int | None
    side: TradeSide | None
    size_text: str | None
    size_low: float | None
    size_high: float | None
    flag_late: bool | None
    flag_executive: bool | None
    flag_10b5_1: bool | None
    form: str | None
    payload_json: str


@dataclass(frozen=True)
class FamilyParse:
    records: tuple[DelayedRecord, ...]  # newest filing (or as-of) date first
    rows_skipped: int  # unparseable, out of window, excluded type, or a duplicate key


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

_USD_NUMBER: Final = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _to_date(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    """UW numbers arrive as strings or numbers; anything else, or non-finite, is None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _to_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    lowered = (_text(value) or "").lower()
    if lowered in {"true", "t", "yes"}:
        return True
    if lowered in {"false", "f", "no"}:
        return False
    return None


def _ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({text for item in value if (text := _text(item)) is not None}))


def _digest(parts: Sequence[str | None]) -> str:
    canonical = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_text(row: Mapping[str, object]) -> str:
    return json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=str)


def _ordered(rows: Sequence[object], records: Iterable[DelayedRecord | None]) -> FamilyParse:
    unique: dict[str, DelayedRecord] = {}
    for record in records:
        if record is not None:
            unique.setdefault(record.dedupe_key, record)
    ordered = sorted(unique.values(), key=lambda r: (-r.filed_or_asof_date.toordinal(), r.dedupe_key))
    return FamilyParse(records=tuple(ordered), rows_skipped=len(rows) - len(ordered))


def _as_of_record(
    symbol: str,
    family: DelayedFamily,
    as_of: date,
    payload: Mapping[str, object],
    *,
    usd: float | None = None,
) -> DelayedRecord:
    return DelayedRecord(
        ticker=symbol,
        family=family,
        dedupe_key=as_of.isoformat(),
        filed_or_asof_date=as_of,
        transaction_date=None,
        delay_days=None,
        side=None,
        size_text=None,
        size_low=usd,
        size_high=usd,
        flag_late=None,
        flag_executive=None,
        flag_10b5_1=None,
        form=None,
        payload_json=_payload_text(payload),
    )


def _usd_range(value: object) -> tuple[str | None, float | None, float | None]:
    """Filed USD range text -> (text, low, high). ``''`` or an unreadable range -> unknown bounds."""
    text = _text(value)
    if text is None:
        return None, None, None
    numbers = [float(raw.replace(",", "")) for raw in _USD_NUMBER.findall(text)]
    if len(numbers) == 2 and numbers[0] <= numbers[1]:
        return text, numbers[0], numbers[1]
    if len(numbers) == 1:
        if "over" in text.lower() or "+" in text:
            return text, numbers[0], None
        return text, numbers[0], numbers[0]
    return text, None, None


def _congress_side(value: object) -> TradeSide | None:
    """Buy/Purchase -> buy; Sell/Sale (Partial)/Sale (Full)/Sell (PARTIAL) -> sell; else None."""
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered.startswith(("buy", "purchase")):
        return "buy"
    if lowered.startswith(("sell", "sale")):
        return "sell"
    return None


def parse_congress_trades(
    data: object,
    *,
    ticker: str,
    today: date,
    settings: DelayedSettings,
) -> FamilyParse:
    """Rows of ``/api/congress/recent-trades`` -> buy/sell records filed inside the lookback."""
    symbol = ticker.strip().upper()
    earliest = today - timedelta(days=settings.congress_lookback_days)
    rows = data if isinstance(data, list) else []
    return _ordered(rows, (_congress_record(r, symbol, earliest, today, settings) for r in rows))


def _congress_record(
    row: object,
    symbol: str,
    earliest: date,
    today: date,
    settings: DelayedSettings,
) -> DelayedRecord | None:
    if not isinstance(row, dict):
        return None
    row_ticker = _text(row.get("ticker"))
    filed = _to_date(row.get("filed_at_date"))
    traded = _to_date(row.get("transaction_date"))
    side = _congress_side(row.get("txn_type"))
    if row_ticker is None or row_ticker.upper() != symbol or filed is None or traded is None:
        return None
    if side is None or filed < traded or not earliest <= filed <= today:
        return None
    size_text, low, high = _usd_range(row.get("amounts"))
    delay = (filed - traded).days
    member_type = (_text(row.get("member_type")) or "").lower()
    key = _digest([
        _text(row.get("politician_id")) or _text(row.get("name")) or _text(row.get("reporter")),
        symbol,
        traded.isoformat(),
        _text(row.get("txn_type")),
        size_text,
        filed.isoformat(),
        _text(row.get("issuer")),
    ])
    return DelayedRecord(
        ticker=symbol,
        family="congress",
        dedupe_key=key,
        filed_or_asof_date=filed,
        transaction_date=traded,
        delay_days=delay,
        side=side,
        size_text=size_text,
        size_low=low,
        size_high=high,
        flag_late=delay > settings.congress_late_days,
        flag_executive=member_type == "executive",
        flag_10b5_1=None,
        form=None,
        payload_json=_payload_text(row),
    )


def parse_insider_transactions(
    data: object,
    *,
    ticker: str,
    today: date,
    settings: DelayedSettings,
) -> FamilyParse:
    """Rows of ``/api/insider/transactions`` -> Form 4/4A P and S records traded inside the lookback."""
    symbol = ticker.strip().upper()
    earliest = today - timedelta(days=settings.insider_lookback_days)
    rows = data if isinstance(data, list) else []
    return _ordered(rows, (_insider_record(r, symbol, earliest, today) for r in rows))


def _insider_key(row: Mapping[str, object]) -> str | None:
    ids = _ids(row.get("ids"))
    if ids:
        return _digest(["ids", *ids])
    row_id = _text(row.get("id"))
    return _digest(["id", row_id]) if row_id is not None else None


def _insider_record(row: object, symbol: str, earliest: date, today: date) -> DelayedRecord | None:
    if not isinstance(row, dict):
        return None
    row_ticker = _text(row.get("ticker"))
    form = _text(row.get("formtype"))
    code = (_text(row.get("transaction_code")) or "").upper()
    if row_ticker is None or row_ticker.upper() != symbol:
        return None
    if form not in INSIDER_FORM_TYPES or code not in _INSIDER_CODE_SIDE:
        return None
    traded = _to_date(row.get("transaction_date"))
    filed = _to_date(row.get("filing_date"))
    if traded is None or filed is None or filed < traded or traded < earliest or filed > today:
        return None
    amount = _to_float(row.get("amount"))
    key = _insider_key(row)
    if amount is None or amount == 0 or key is None:
        return None
    sign_side: TradeSide = "buy" if amount > 0 else "sell"
    price = _to_float(row.get("price"))
    size = abs(amount) * price if price is not None and price > 0 else None
    return DelayedRecord(
        ticker=symbol,
        family="insider",
        dedupe_key=key,
        filed_or_asof_date=filed,
        transaction_date=traded,
        delay_days=(filed - traded).days,
        side=sign_side if sign_side == _INSIDER_CODE_SIDE[code] else None,
        size_text=None,
        size_low=size,
        size_high=size,
        flag_late=None,
        flag_executive=None,
        flag_10b5_1=_to_bool(row.get("is_10b5_1")),
        form=form,
        payload_json=_payload_text(row),
    )


def parse_short_interest(
    data: object,
    *,
    ticker: str,
    today: date,
    settings: DelayedSettings,
) -> FamilyParse:
    """Rows of ``/api/shorts/{t}/interest-float/v2`` -> one record per as-of date inside the window."""
    symbol = ticker.strip().upper()
    earliest = today - timedelta(days=settings.short_interest_lookback_days)
    rows = data if isinstance(data, list) else []
    return _ordered(rows, (_short_interest_record(r, symbol, earliest, today) for r in rows))


def _short_interest_record(row: object, symbol: str, earliest: date, today: date) -> DelayedRecord | None:
    if not isinstance(row, dict):
        return None
    row_symbol = _text(row.get("symbol"))
    as_of = _to_date(row.get("market_date"))
    si_float = _to_float(row.get("si_float"))
    if row_symbol is not None and row_symbol.upper() != symbol:
        return None
    if as_of is None or not earliest <= as_of <= today or si_float is None or si_float < 0:
        return None
    kept = {k: v for k, v in row.items() if k not in _IGNORED_SHORT_INTEREST_FIELDS}
    return _as_of_record(symbol, "short_interest", as_of, kept)


def parse_ftds(
    data: object,
    *,
    ticker: str,
    today: date,
    settings: DelayedSettings,
) -> FamilyParse:
    """Rows of ``/api/shorts/{t}/ftds`` -> one record per fail date inside the trailing window."""
    symbol = ticker.strip().upper()
    earliest = today - timedelta(days=settings.ftd_lookback_days)
    rows = data if isinstance(data, list) else []
    return _ordered(rows, (_ftd_record(r, symbol, earliest, today) for r in rows))


def _ftd_record(row: object, symbol: str, earliest: date, today: date) -> DelayedRecord | None:
    if not isinstance(row, dict):
        return None
    fail_day = _to_date(row.get("date"))
    quantity = _to_float(row.get("quantity"))
    if fail_day is None or not earliest <= fail_day <= today or quantity is None or quantity <= 0:
        return None
    price = _to_float(row.get("price"))
    notional = quantity * price if price is not None and price > 0 else None
    return _as_of_record(symbol, "ftd", fail_day, row, usd=notional)


# ---------------------------------------------------------------------------
# Daily job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelayedFamilyResult:
    ticker: str
    family: DelayedFamily
    status: FetchStatus
    inserted: int = 0
    already_stored: int = 0
    rows_skipped: int = 0
    truncated: bool = False  # the source signalled more rows than it returned


@dataclass(frozen=True)
class DelayedJobResult:
    results: tuple[DelayedFamilyResult, ...]

    @property
    def degraded(self) -> tuple[tuple[str, DelayedFamily], ...]:
        return tuple((r.ticker, r.family) for r in self.results if r.status == "degraded")


class _FamilyJob(Protocol):
    async def __call__(
        self,
        client: JsonClient,
        factory: sessionmaker[Session],
        ticker: str,
        *,
        today: date,
        fetched_at: datetime,
        settings: DelayedSettings,
    ) -> DelayedFamilyResult: ...


async def run_delayed_job(
    client: JsonClient,
    engine: Engine,
    tickers: Iterable[str],
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedJobResult:
    """Fetch every delayed family for every ticker and append the rows not stored yet.

    ``now`` is the job's event time (D9): its ET date bounds the lookback windows
    and it is stored as ``fetched_at``. One request per (ticker, family), in the
    order congress, insider, short interest, FTD. DailyLimit and Auth errors
    propagate; rows already committed stay.
    """
    today, fetched_at = _clock(now)
    ensure_delayed_tables(engine)
    factory = session_factory(engine)
    results: list[DelayedFamilyResult] = []
    for ticker in _normalized(tickers):
        for job in _FAMILY_JOBS:
            results.append(
                await job(client, factory, ticker, today=today, fetched_at=fetched_at, settings=settings),
            )
    return DelayedJobResult(results=tuple(results))


async def refresh_congress(
    client: JsonClient,
    engine: Engine,
    ticker: str,
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    """The congress family alone for one ticker (one request)."""
    return await _refresh_one(_congress, client, engine, ticker, now=now, settings=settings)


async def refresh_insider(
    client: JsonClient,
    engine: Engine,
    ticker: str,
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    """The insider family alone for one ticker (one request)."""
    return await _refresh_one(_insider, client, engine, ticker, now=now, settings=settings)


async def refresh_short_interest(
    client: JsonClient,
    engine: Engine,
    ticker: str,
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    """The short-interest family alone for one ticker (one request)."""
    return await _refresh_one(_short_interest, client, engine, ticker, now=now, settings=settings)


async def refresh_ftds(
    client: JsonClient,
    engine: Engine,
    ticker: str,
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    """The FTD family alone for one ticker (one request)."""
    return await _refresh_one(_ftds, client, engine, ticker, now=now, settings=settings)


async def _refresh_one(
    job: _FamilyJob,
    client: JsonClient,
    engine: Engine,
    ticker: str,
    *,
    now: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    today, fetched_at = _clock(now)
    ensure_delayed_tables(engine)
    return await job(
        client, session_factory(engine), ticker.strip().upper(),
        today=today, fetched_at=fetched_at, settings=settings,
    )


def _clock(now: datetime) -> tuple[date, datetime]:
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    return now.astimezone(_ET).date(), now.astimezone(UTC)


def _normalized(tickers: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in tickers:
        symbol = raw.strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return tuple(out)


async def _get(
    client: JsonClient,
    path: str,
    params: dict[str, Any] | None,
    *,
    what: str,
) -> tuple[FetchStatus, dict[str, Any] | None]:
    try:
        return "ok", await client.request_json(path, params=params)
    except UnusualWhalesNotFoundError:
        return "no_data", None
    except UnusualWhalesDailyLimitError:
        raise
    except (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError) as exc:
        _logger.warning("delayed fetch degraded (%s): %s", what, exc)
        return "degraded", None


def _rows(body: Mapping[str, object] | None) -> list[object]:
    """The ``data`` rows of a body. A single object (the spec's example shape) counts as one row."""
    if body is None:
        return []
    data = body.get("data")
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


def _empty(ticker: str, family: DelayedFamily, status: FetchStatus, body: object) -> DelayedFamilyResult:
    return DelayedFamilyResult(ticker=ticker, family=family, status=status if body is None else "no_data")


def _finish(
    factory: sessionmaker[Session],
    ticker: str,
    family: DelayedFamily,
    parsed: FamilyParse,
    fetched_at: datetime,
    *,
    truncated: bool,
) -> DelayedFamilyResult:
    if truncated:
        _logger.warning("delayed %s source for %s signalled more rows than returned", family, ticker)
    inserted, existing = _store(factory, ticker, family, parsed.records, fetched_at)
    return DelayedFamilyResult(
        ticker=ticker,
        family=family,
        status="ok",
        inserted=inserted,
        already_stored=existing,
        rows_skipped=parsed.rows_skipped,
        truncated=truncated,
    )


async def _congress(
    client: JsonClient,
    factory: sessionmaker[Session],
    ticker: str,
    *,
    today: date,
    fetched_at: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    status, body = await _get(
        client,
        CONGRESS_RECENT_TRADES_PATH,
        {"ticker": ticker, "limit": _CONGRESS_LIMIT},
        what=f"congress {ticker}",
    )
    rows = _rows(body)
    if not rows:
        return _empty(ticker, "congress", status, body)
    parsed = parse_congress_trades(rows, ticker=ticker, today=today, settings=settings)
    return _finish(factory, ticker, "congress", parsed, fetched_at, truncated=len(rows) >= _CONGRESS_LIMIT)


async def _insider(
    client: JsonClient,
    factory: sessionmaker[Session],
    ticker: str,
    *,
    today: date,
    fetched_at: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    start = today - timedelta(days=settings.insider_lookback_days)
    status, body = await _get(
        client,
        INSIDER_TRANSACTIONS_PATH,
        {
            "ticker_symbol": ticker,
            "form_types[]": list(INSIDER_FORM_TYPES),
            "start_date": start.isoformat(),
        },
        what=f"insider {ticker}",
    )
    rows = _rows(body)
    if body is None or not rows:
        return _empty(ticker, "insider", status, body)
    parsed = parse_insider_transactions(rows, ticker=ticker, today=today, settings=settings)
    return _finish(factory, ticker, "insider", parsed, fetched_at, truncated=body.get("has_more") is True)


async def _short_interest(
    client: JsonClient,
    factory: sessionmaker[Session],
    ticker: str,
    *,
    today: date,
    fetched_at: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    status, body = await _get(
        client, SHORT_INTEREST_PATH.format(ticker=ticker), None, what=f"short interest {ticker}",
    )
    rows = _rows(body)
    if not rows:
        return _empty(ticker, "short_interest", status, body)
    parsed = parse_short_interest(rows, ticker=ticker, today=today, settings=settings)
    return _finish(factory, ticker, "short_interest", parsed, fetched_at, truncated=False)


async def _ftds(
    client: JsonClient,
    factory: sessionmaker[Session],
    ticker: str,
    *,
    today: date,
    fetched_at: datetime,
    settings: DelayedSettings,
) -> DelayedFamilyResult:
    status, body = await _get(client, FTDS_PATH.format(ticker=ticker), None, what=f"ftds {ticker}")
    rows = _rows(body)
    if not rows:
        return _empty(ticker, "ftd", status, body)
    parsed = parse_ftds(rows, ticker=ticker, today=today, settings=settings)
    return _finish(factory, ticker, "ftd", parsed, fetched_at, truncated=False)


_FAMILY_JOBS: Final[tuple[_FamilyJob, ...]] = (_congress, _insider, _short_interest, _ftds)


def _store(
    factory: sessionmaker[Session],
    ticker: str,
    family: DelayedFamily,
    records: Sequence[DelayedRecord],
    fetched_at: datetime,
) -> tuple[int, int]:
    if not records:
        return 0, 0
    try:
        return _insert_missing(factory, ticker, family, records, fetched_at)
    except IntegrityError:
        # Another writer stored some of these keys between our read and commit.
        _logger.warning("concurrent write on alfa_delayed for %s/%s; re-reading once", ticker, family)
        return _insert_missing(factory, ticker, family, records, fetched_at)


def _insert_missing(
    factory: sessionmaker[Session],
    ticker: str,
    family: DelayedFamily,
    records: Sequence[DelayedRecord],
    fetched_at: datetime,
) -> tuple[int, int]:
    """Insert the records whose key is not stored yet. Existing rows are untouched."""
    with factory() as session:
        stored = set(
            session.execute(
                select(AlfaDelayed.dedupe_key).where(
                    AlfaDelayed.ticker == ticker, AlfaDelayed.family == family,
                ),
            ).scalars(),
        )
        fresh = [r for r in records if r.dedupe_key not in stored]
        session.add_all(_row(r, fetched_at) for r in fresh)
        session.commit()
    return len(fresh), len(records) - len(fresh)


def _row(record: DelayedRecord, fetched_at: datetime) -> AlfaDelayed:
    return AlfaDelayed(
        ticker=record.ticker,
        family=record.family,
        dedupe_key=record.dedupe_key,
        filed_or_asof_date=record.filed_or_asof_date,
        transaction_date=record.transaction_date,
        delay_days=record.delay_days,
        side=record.side,
        size_text=record.size_text,
        size_low=record.size_low,
        size_high=record.size_high,
        flag_late=record.flag_late,
        flag_executive=record.flag_executive,
        flag_10b5_1=record.flag_10b5_1,
        form=record.form,
        payload_json=record.payload_json,
        fetched_at=fetched_at,
    )


def load_delayed_records(engine: Engine, ticker: str) -> tuple[DelayedRecord, ...]:
    """Every stored delayed row of ``ticker``. Read-only; unknown families are ignored."""
    stmt = select(AlfaDelayed).where(AlfaDelayed.ticker == ticker.strip().upper())
    with session_factory(engine)() as session:
        rows = session.execute(stmt).scalars().all()
        return tuple(record for row in rows if (record := _record(row)) is not None)


def _record(row: AlfaDelayed) -> DelayedRecord | None:
    family = next((f for f in _FAMILY_ORDER if f == row.family), None)
    if family is None:
        return None
    side = next((s for s in get_args(TradeSide) if s == row.side), None)
    return DelayedRecord(
        ticker=row.ticker,
        family=family,
        dedupe_key=row.dedupe_key,
        filed_or_asof_date=row.filed_or_asof_date,
        transaction_date=row.transaction_date,
        delay_days=row.delay_days,
        side=cast(TradeSide | None, side),
        size_text=row.size_text,
        size_low=row.size_low,
        size_high=row.size_high,
        flag_late=row.flag_late,
        flag_executive=row.flag_executive,
        flag_10b5_1=row.flag_10b5_1,
        form=row.form,
        payload_json=row.payload_json,
    )


# ---------------------------------------------------------------------------
# Presentation (pure). Never counted: no type here has a count field.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelayedOutcome:
    """Underlying move since the filing (or as-of) date; unsigned and never scored."""

    known: bool
    pct: float | None  # percent units
    base_day: date | None  # close used as the base (on or before the filing date)
    through_day: date | None  # newest close on or before today
    text: str


@dataclass(frozen=True)
class DelayedItem:
    key: str
    family: DelayedFamily
    family_label: str
    date_label: str
    filed_or_asof_date: date
    transaction_date: date | None
    delay_days: int  # filed - transaction; for the shorts families today - as-of (or fail) date
    delay_text: str
    side: TradeSide | None
    side_text: str | None
    size_text: str | None  # congress: the filed USD range as-is; otherwise generated copy
    size_low_usd: float | None
    size_high_usd: float | None
    flags: tuple[str, ...]
    who: str | None  # vendor data (filer name and title), not generated copy
    outcome: DelayedOutcome


@dataclass(frozen=True)
class DelayedEvidence:
    """A ticker's delayed items for the ``ek kanıt (gecikmeli)`` bucket, newest first."""

    ticker: str
    bucket_label: str
    exclusion_note: str
    items: tuple[DelayedItem, ...]


def build_delayed_evidence(
    ticker: str,
    records: Iterable[DelayedRecord],
    closes: Sequence[ClosePoint],
    *,
    today: date,
    settings: DelayedSettings,
) -> DelayedEvidence:
    """Delayed items of ``ticker`` inside their lookback windows as of ``today`` (D9)."""
    symbol = ticker.strip().upper()
    mine = [record for record in records if record.ticker == symbol]
    hidden = _superseded_insider_groups(mine)
    items = [
        _item(record, closes, today)
        for record in mine
        if record.dedupe_key not in hidden and _in_window(record, today, settings)
    ]
    items.sort(key=lambda i: (
        -i.filed_or_asof_date.toordinal(), _FAMILY_ORDER.index(i.family), i.key,
    ))
    return DelayedEvidence(
        ticker=symbol,
        bucket_label=_say("bucket"),
        exclusion_note=_say("exclusion"),
        items=tuple(items),
    )


def load_delayed_evidence(
    engine: Engine,
    ticker: str,
    *,
    today: date,
    settings: DelayedSettings,
) -> DelayedEvidence:
    """Read-only: stored delayed rows and daily closes -> the ticker's delayed items."""
    return build_delayed_evidence(
        ticker,
        load_delayed_records(engine, ticker),
        load_closes(engine, ticker),
        today=today,
        settings=settings,
    )


def _superseded_insider_groups(records: Sequence[DelayedRecord]) -> set[str]:
    """Keys of insider groups whose ids are a strict subset of another stored group's ids."""
    groups = [
        (record.dedupe_key, frozenset(ids))
        for record in records
        if record.family == "insider" and (ids := _ids(_payload(record.payload_json).get("ids")))
    ]
    return {key for key, ids in groups if any(ids < other for _, other in groups)}


def _in_window(record: DelayedRecord, today: date, settings: DelayedSettings) -> bool:
    dated = record.filed_or_asof_date
    if dated > today:
        return False
    if record.family == "congress":
        return record.transaction_date is not None and dated >= today - timedelta(
            days=settings.congress_lookback_days,
        )
    if record.family == "insider":
        return record.transaction_date is not None and record.transaction_date >= today - timedelta(
            days=settings.insider_lookback_days,
        )
    if record.family == "short_interest":
        return dated >= today - timedelta(days=settings.short_interest_lookback_days)
    return dated >= today - timedelta(days=settings.ftd_lookback_days)


def _item(record: DelayedRecord, closes: Sequence[ClosePoint], today: date) -> DelayedItem:
    dated = record.filed_or_asof_date
    kind = _DATE_KIND[record.family]
    if kind == "filed":
        delay = (dated - (record.transaction_date or dated)).days
    else:
        delay = (today - dated).days
    payload = _payload(record.payload_json)
    flags: list[str] = []
    if record.flag_late:
        flags.append(_say("flag.late"))
    if record.flag_executive:
        flags.append(_say("flag.executive"))
    if record.flag_10b5_1:
        flags.append(_say("flag.10b5_1"))
    if record.form == _AMENDED_FORM:
        flags.append(_say("flag.amended"))
    return DelayedItem(
        key=record.dedupe_key,
        family=record.family,
        family_label=_say(f"family.{record.family}"),
        date_label=_say(f"date.{kind}"),
        filed_or_asof_date=dated,
        transaction_date=record.transaction_date,
        delay_days=delay,
        delay_text=_say(f"delay.{kind}", days=delay),
        side=record.side,
        side_text=_say(f"side.{record.side}") if record.side is not None else None,
        size_text=_size_text(record, payload),
        size_low_usd=record.size_low,
        size_high_usd=record.size_high,
        flags=tuple(flags),
        who=_who(record, payload),
        outcome=_outcome(closes, dated, today, kind),
    )


def _payload(payload_json: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(payload_json)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _who(record: DelayedRecord, payload: Mapping[str, object]) -> str | None:
    if record.family == "insider":
        name = _text(payload.get("owner_name"))
        title = _text(payload.get("officer_title"))
        return f"{name} ({title})" if name is not None and title is not None else name
    if record.family == "congress":
        return _text(payload.get("name")) or _text(payload.get("reporter"))
    return None


def _size_text(record: DelayedRecord, payload: Mapping[str, object]) -> str | None:
    if record.family == "congress":
        return record.size_text
    if record.family == "short_interest":
        si_float = _to_float(payload.get("si_float"))
        if si_float is None:
            return None
        pct = f"{si_float * 100:.2f}"  # fraction -> percent
        days_to_cover = _to_float(payload.get("days_to_cover"))
        if days_to_cover is None:
            return _say("size.short_interest_no_dtc", pct=pct)
        return _say("size.short_interest", pct=pct, dtc=f"{days_to_cover:.2f}")
    shares = _to_float(payload.get("amount" if record.family == "insider" else "quantity"))
    price = _to_float(payload.get("price"))
    if shares is None or price is None or record.size_low is None:
        return None
    return _say(
        "size.shares",
        shares=f"{abs(shares):,.0f}",
        price=f"{price:,.2f}",
        usd=f"{record.size_low:,.0f}",
    )


def _outcome(closes: Sequence[ClosePoint], base: date, today: date, kind: str) -> DelayedOutcome:
    move = pct_move_between(closes, base, today)
    if move is None or move.end.day <= move.start.day:
        return DelayedOutcome(
            known=False, pct=None, base_day=None, through_day=None, text=_say("outcome.unknown"),
        )
    return DelayedOutcome(
        known=True,
        pct=move.pct,
        base_day=move.start.day,
        through_day=move.end.day,
        text=_say(
            f"outcome.{kind}",
            pct=f"{move.pct:+.1f}",
            base=move.start.day.isoformat(),
            through=move.end.day.isoformat(),
        ),
    )
