"""Unusual Whales OpenInterestProvider implementation.

Phase 3.3.3.5 introduced ``OpenInterestProvider`` with ``at()`` and
``next_day()``. Phase 3.9.9 moves both onto the live-verified
per-contract historic endpoint
(``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.6).

  - Module 27 (Opening vs Closing Interest Estimator) calls ``at()``
    twice: at the prior session close and at the event.
  - Module 28 (Next-Day OI Confirmation) calls ``next_day()`` for the
    OI after the trade day, and ``at()`` at the signal as baseline.

UW endpoint shape (verified live 2026-09-14):

  GET /api/option-contract/{OCC}/historic
    -> {"chains": [{"date": "2026-09-14", "open_interest": 104505,
                    "volume": 0,
                    "last_tape_time": "2026-09-14T10:30:18Z", ...},
                   {"date": "2026-09-11", "open_interest": 106553,
                    "volume": 9324, ...},
                   ...],
        "etf_holdings": [...]}

  One row per trading day for the whole life of the contract, newest
  first (143 rows for AAPL261016C00340000). The ``date`` query
  parameter is ignored by the server (identical payload with and
  without it), so no query parameters are sent.

decision (start-of-day OI semantics):
  Row D carries the OI after D-1's trading, and it is published before
  D's open. Live evidence: row 2026-09-14 was present pre-market
  (``last_tape_time`` 10:30Z, volume 0); row 2026-09-11 holds 106553,
  which matches ``/api/stock/{t}/oi-change`` (curr_date 2026-09-11,
  curr_oi 106553, last_oi 15611, volume 111857 = row 2026-09-10's
  volume). Row D is therefore knowable at any instant of ET date D,
  and ``at(when)`` returns the latest row with
  ``date <= ET date(when)`` without look-ahead. ``as_of`` is 00:00
  America/New_York of the row date, expressed in UTC. A weekend or
  holiday ``when`` returns the last trading day's row. A naive
  ``when`` (the Protocol requires tz-aware UTC) is read as UTC so the
  result never depends on the host timezone.

decision (next_day = earliest row after trade_date):
  ``next_day(trade_date)`` returns the earliest row with
  ``date > trade_date``: the OI after ``trade_date``'s trading. Weekends
  and holidays are skipped by construction (Friday -> Monday's row). It
  returns None while that row is not published, so M28 keeps the
  signal pending. M28's baseline ``at(signal.timestamp)`` is the
  trade day's own start-of-day row, so its delta is the OI change
  over the trade day.

M27 interpretation note (contract §3.6 and §4; flagged for Berkay, no
stage change):
  M27 compares ``at(prior session close)`` with ``at(event)``. Under
  start-of-day OI that is row D-1 against row D, i.e. the OI built
  during D-1's trading. Opening on the event day itself is not
  observable in daily OI.

decision (one cache per OCC symbol serves both methods):
  The endpoint returns every day of the contract, so one fetch answers
  ``at()`` for any ``when`` and ``next_day()`` for any ``trade_date``.
  The cache holds the parsed (date, open_interest) rows keyed by OCC
  symbol, on the ``open_interest_seconds`` TTL. Rows published after
  a fetch become visible once that entry expires.

decision (not ported from origin/phase-3):
  phase-3 took ``chains[0]`` for every request. ``chains[0]`` is always
  today's row, so prior and current OI were always equal.

decision (404/422 -> None, cached as empty):
  ``UnusualWhalesNotFoundError`` (unknown or malformed contract) means
  the symbol has no data. It is cached as an empty row set so a
  missing contract is not refetched within the TTL. Every other client
  error (401/403, 429 after retries, 5xx, open breaker) propagates: a
  bad key must never look like "no data", and M28 counts a raise as an
  error rather than as pending.

decision (malformed rows are skipped):
  A row whose ``date`` is not a ``YYYY-MM-DD`` string, or whose
  ``open_interest`` is missing, boolean or not an integer (an integer
  string is accepted), is dropped; the other rows still answer. Rows
  are read from ``chains``; ``data`` is tolerated because the client
  wraps top-level arrays as ``{"data": [...]}``. If a date appears
  twice, the first row in response order wins.

Rejected alternatives (contract §3.6): ``/api/stock/{t}/oi-per-strike``
sums OI across expiries at a strike, destroying contract-level deltas;
``/api/stock/{t}/oi-change`` returns every contract of the ticker per
call, too large for M27's provider timeout.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from uoa_detector.providers.open_interest import OpenInterestSnapshot
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesNotFoundError,
)
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


# Exchange timezone: row dates and ``as_of`` are America/New_York days.
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _OIRow:
    """Start-of-day OI of one contract on ``day`` (after day-1's trading)."""

    day: date
    open_interest: int


class UnusualWhalesOpenInterestProvider:
    """``OpenInterestProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[tuple[_OIRow, ...]] = TTLCache(
            ttl_seconds=settings.cache_ttl.open_interest_seconds,
        )

    async def at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        when: datetime,
    ) -> OpenInterestSnapshot | None:
        """Return the latest row dated on or before the ET date of ``when``."""
        rows = await self._rows(_occ_symbol(ticker, expiry, option_type, strike))
        idx = bisect_right(rows, _et_date(when), key=_row_day)
        if idx == 0:
            return None
        return _to_snapshot(
            rows[idx - 1],
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
        )

    async def next_day(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        trade_date: date,
    ) -> OpenInterestSnapshot | None:
        """Return the earliest row dated after ``trade_date``, or None."""
        rows = await self._rows(_occ_symbol(ticker, expiry, option_type, strike))
        idx = bisect_right(rows, trade_date, key=_row_day)
        if idx == len(rows):
            return None
        return _to_snapshot(
            rows[idx],
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
        )

    async def _rows(self, symbol: str) -> tuple[_OIRow, ...]:
        return await self._cache.get_or_fetch(
            symbol,
            loader=lambda: self._fetch_rows(symbol),
        )

    async def _fetch_rows(self, symbol: str) -> tuple[_OIRow, ...]:
        path = f"/api/option-contract/{symbol}/historic"
        try:
            resp = await self._client.request_json(path)
        except UnusualWhalesNotFoundError:
            return ()
        return _parse_rows(resp)


def _parse_rows(resp: dict[str, Any]) -> tuple[_OIRow, ...]:
    """Parse ``chains`` (or ``data``) into rows sorted by date ascending."""
    raw = resp.get("chains")
    if not isinstance(raw, list):
        raw = resp.get("data")
    if not isinstance(raw, list):
        return ()
    by_day: dict[date, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = _parse_day(item.get("date"))
        open_interest = _parse_open_interest(item.get("open_interest"))
        if day is None or open_interest is None:
            continue
        by_day.setdefault(day, open_interest)
    return tuple(
        _OIRow(day=day, open_interest=oi) for day, oi in sorted(by_day.items())
    )


def _parse_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_open_interest(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _row_day(row: _OIRow) -> date:
    return row.day


def _et_date(when: datetime) -> date:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(_ET).date()


def _to_snapshot(
    row: _OIRow,
    *,
    ticker: str,
    strike: Decimal,
    expiry: date,
    option_type: Literal["call", "put"],
) -> OpenInterestSnapshot:
    as_of = datetime.combine(row.day, time(0, 0), tzinfo=_ET).astimezone(UTC)
    return OpenInterestSnapshot(
        ticker=ticker.upper(),
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        as_of=as_of,
        open_interest=row.open_interest,
    )


def _occ_symbol(
    ticker: str, expiry: date, option_type: Literal["call", "put"],
    strike: Decimal,
) -> str:
    yymmdd = f"{expiry.year % 100:02d}{expiry.month:02d}{expiry.day:02d}"
    cp = "C" if option_type == "call" else "P"
    strike_int = int(strike * Decimal(1000))
    return f"{ticker.upper()}{yymmdd}{cp}{strike_int:08d}"
