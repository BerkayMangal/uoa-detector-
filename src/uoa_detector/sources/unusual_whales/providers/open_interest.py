"""Unusual Whales OpenInterestProvider implementation.

Phase 3.3.3.5: implements ``OpenInterestProvider`` Protocol with
both ``at()`` (intraday/historical) and ``next_day()`` (T+1
post-market validation) methods.

  - Module 27 (Opening vs Closing Interest Estimator) calls ``at()``
    to compare per-event OI against prior-close OI.
  - Module 28 (Next-Day OI Confirmation) calls ``next_day()`` after
    market close T+1 to validate Module 27's classification (the
    OI delta tells us whether the flow opened or closed positioning).

The provider is pure data-shipping. Both methods return None if UW
has no snapshot for the requested key.

UW endpoint shapes:

  GET /api/option-contract/{symbol}/open-interest?at={iso}
    → {"data": [{"as_of": "...", "open_interest": 12345}]}

  GET /api/option-contract/{symbol}/open-interest/eod?date={yyyy-mm-dd}
    → {"data": {"as_of": "...", "open_interest": 12345}}

decision (separate cache for at() vs next_day()):
  Different key shapes, different TTL semantics. ``at()`` queries
  intraday snapshots that turn over often; ``next_day()`` queries
  EOD authoritative numbers that don't change. Same TTL value but
  conceptually distinct entries — they share the
  ``open_interest_seconds`` setting.

decision (next_day() takes trade_date, queries trade_date+1 EOD):
  The Protocol's ``trade_date`` parameter is the day the flow
  happened. The 'next day' OI is the EOD snapshot from the
  following business day. We compute trade_date+1 here and let
  the operator (Phase 3.4 wiring) handle weekend/holiday
  adjustments — this provider doesn't know the calendar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from uoa_detector.providers.open_interest import OpenInterestSnapshot
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


class UnusualWhalesOpenInterestProvider:
    """``OpenInterestProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl = settings.cache_ttl.open_interest_seconds
        self._cache_at: TTLCache[dict[str, Any] | None] = TTLCache(
            ttl_seconds=ttl,
        )
        self._cache_eod: TTLCache[dict[str, Any] | None] = TTLCache(
            ttl_seconds=ttl,
        )

    async def at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        when: datetime,
    ) -> OpenInterestSnapshot | None:
        """Return the OI snapshot at ``when``, or None."""
        symbol = _occ_symbol(ticker, expiry, option_type, strike)
        # Cache key includes the date portion of 'when' so different
        # days don't share an entry.
        key = (symbol, when.date().isoformat())
        row = await self._cache_at.get_or_fetch(
            key,
            loader=lambda: self._fetch_at(symbol, when),
        )
        return _row_to_oi_snapshot(
            row,
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
        """Return the EOD OI snapshot for trade_date+1."""
        symbol = _occ_symbol(ticker, expiry, option_type, strike)
        next_d = trade_date + timedelta(days=1)
        key = (symbol, next_d.isoformat())
        row = await self._cache_eod.get_or_fetch(
            key,
            loader=lambda: self._fetch_eod(symbol, next_d),
        )
        return _row_to_oi_snapshot(
            row,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
        )

    async def _fetch_at(
        self, symbol: str, when: datetime,
    ) -> dict[str, Any] | None:
        params = {"at": when.isoformat()}
        path = f"/api/option-contract/{symbol}/open-interest"
        resp = await self._client.request_json(path, params=params)
        data = resp.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        if isinstance(data, dict):
            return data
        return None

    async def _fetch_eod(
        self, symbol: str, on_date: date,
    ) -> dict[str, Any] | None:
        params = {"date": on_date.isoformat()}
        path = f"/api/option-contract/{symbol}/open-interest/eod"
        resp = await self._client.request_json(path, params=params)
        data = resp.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        return None


def _row_to_oi_snapshot(
    row: dict[str, Any] | None,
    *,
    ticker: str,
    strike: Decimal,
    expiry: date,
    option_type: Literal["call", "put"],
) -> OpenInterestSnapshot | None:
    if row is None:
        return None
    try:
        as_of = _parse_iso_utc(str(row["as_of"]))
        open_interest = int(row["open_interest"])
    except (KeyError, ValueError, TypeError):
        return None
    return OpenInterestSnapshot(
        ticker=ticker.upper(),
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        as_of=as_of,
        open_interest=open_interest,
    )


def _occ_symbol(
    ticker: str, expiry: date, option_type: Literal["call", "put"],
    strike: Decimal,
) -> str:
    yymmdd = f"{expiry.year % 100:02d}{expiry.month:02d}{expiry.day:02d}"
    cp = "C" if option_type == "call" else "P"
    strike_int = int(strike * Decimal(1000))
    return f"{ticker.upper()}{yymmdd}{cp}{strike_int:08d}"


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
