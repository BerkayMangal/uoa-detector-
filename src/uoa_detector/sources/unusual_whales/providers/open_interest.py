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

UW endpoint shape (Phase 3.3.9.5 — migrated; the previous
``/open-interest`` and ``/open-interest/eod`` endpoints were
retired in favour of a single per-contract ``/historic`` endpoint
that publishes daily chain data including OI):

  GET /api/option-contract/{symbol}/historic?date={yyyy-mm-dd}
    → {"chains": [{"date": "2026-05-11",
                    "open_interest": 32224,
                    "volume": 9988,
                    "implied_volatility": "0.276195801864905",
                    "last_price": "4.90",
                    "nbbo_bid": "4.75", "nbbo_ask": "5.00",
                    "last_tape_time": "2026-05-11T21:37:17Z",
                    ...}, ...]}

decision (Phase 3.3.9.5 — OI is daily-granular only):
  UW publishes a single OI value per (contract, calendar day) —
  the prior-day EOD official open interest. There's no intraday
  OI tick stream on the public API. ``at(when)`` therefore
  returns the EOD snapshot whose ``date`` is ``when.date()``;
  the OpenInterestSnapshot's ``as_of`` is set to the chain's
  ``last_tape_time`` (preferred) or the date cast to 21:00 UTC.

decision (top-level key is ``chains`` not ``data``):
  Unique to the historic endpoint among UW's data-shop responses.
  The ``_coerce_chains_list`` helper accepts both shapes so
  legacy fixtures (``{"data": [...]}``) continue to work for
  back-compat in Phase 3.3.3 unit tests.

decision (separate cache for at() vs next_day()):
  Unchanged in form. ``at()`` keys on (symbol, when.date());
  ``next_day()`` keys on (symbol, trade_date+1). Both share the
  ``open_interest_seconds`` TTL.

decision (next_day() takes trade_date, queries trade_date+1):
  Unchanged from Phase 3.3.3. The provider computes trade_date+1
  in calendar days; weekend/holiday adjustment remains the
  operator's responsibility (Phase 3.5+ trading-calendar overlay).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as _time
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
        params = {"date": when.date().isoformat()}
        path = f"/api/option-contract/{symbol}/historic"
        resp = await self._client.request_json(path, params=params)
        return _coerce_first_chain(resp)

    async def _fetch_eod(
        self, symbol: str, on_date: date,
    ) -> dict[str, Any] | None:
        params = {"date": on_date.isoformat()}
        path = f"/api/option-contract/{symbol}/historic"
        resp = await self._client.request_json(path, params=params)
        return _coerce_first_chain(resp)


def _coerce_first_chain(resp: object) -> dict[str, Any] | None:
    """Phase 3.3.9.5: pick the first chain row from either schema.

    New UW historic endpoint: ``{"chains": [{...}, ...]}``.
    Legacy fixtures may still ship ``{"data": [{...}]}`` (list) or
    ``{"data": {...}}`` (dict).
    """
    if not isinstance(resp, dict):
        return None
    chains = resp.get("chains")
    if isinstance(chains, list) and chains:
        first = chains[0]
        return first if isinstance(first, dict) else None
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
        as_of = _row_as_of(row)
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


def _row_as_of(row: dict[str, Any]) -> datetime:
    """Phase 3.3.9.5: prefer ``last_tape_time``, fall back to ``date``
    at 21:00 UTC. Legacy ``as_of`` ISO field still honoured.
    """
    if "last_tape_time" in row:
        return _parse_iso_utc(str(row["last_tape_time"]))
    if "as_of" in row:
        return _parse_iso_utc(str(row["as_of"]))
    date_str = str(row["date"])
    day = date.fromisoformat(date_str)
    return datetime.combine(day, _time(hour=21, minute=0), tzinfo=UTC)


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
