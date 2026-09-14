"""Unusual Whales ``DarkPoolPrintProvider`` implementation (feeds Module 26).

Phase 3.9.7 (contract ``docs/phase-3.9-uw-endpoint-correction-acceptance.md``
§3.4). The Phase 3.3.3 path ``/api/darkpool/{ticker}/prints`` returned
HTTP 404. The live endpoint, verified 2026-09-14:

  GET /api/darkpool/{ticker}
      ?date=<ET date of before>
      &newer_than=<ISO-8601 UTC>&older_than=<ISO-8601 UTC>
      &limit=500&order_by=premium&order=desc
    -> {"data": [
         {"executed_at": "2026-09-11T18:48:54Z",
          "trf_executed_at": "2026-09-11T18:48:54Z",
          "price": "765.06", "size": 20000, "premium": "15301200.00",
          "nbbo_bid": "765.06", "nbbo_ask": "765.08",
          "canceled": false, "sale_cond_codes": null, "trade_code": null,
          "ext_hour_sold_codes": null, "market_center": "L", ...},
         ...]}

ISO ``newer_than`` / ``older_than`` are honoured on this endpoint (unlike
flow-alerts, which needs epoch seconds). Rows come back premium-descending
and a 500-row response is a truncated page.

The provider is pure data-shipping; Module 26 applies its own scoring.

decision (hour-bucket fetch, cache key ``(ticker, bucket, window)``):
  ``bucket = floor_hour_utc(before)``. One request covers
  ``[bucket - window, bucket + 1h]``, so every event in the same UTC hour
  with the same lookback shares one fetch. The ET date of ``before`` is
  constant within a UTC hour (ET offsets are whole hours), so it is not
  part of the key. The ``date`` filter clips a window that crosses ET
  midnight; prints at that hour are extended-hours prints, which are
  ``unknown`` side anyway.

decision (client-side look-ahead filter, always applied):
  The bucket reaches past ``before``, so cached rows are filtered on every
  call: ``before - window <= executed_at <= before``. ``trf_executed_at``
  (the tape report time) must be ``<= before`` when present, because a
  print is not knowable before it is reported; an unparseable
  ``trf_executed_at`` drops the row like any other malformed timestamp.
  Null ``trf_executed_at`` (UW: trades before 2025-05-01) falls back to
  ``executed_at`` alone. ``canceled is True`` prints never traded and are
  skipped.

decision (truncation):
  A full page (``_PAGE_LIMIT`` rows) logs a WARNING. Premium-descending
  order keeps the largest prints, which are the ones M26 qualifies on. On
  a full page, whether a smaller in-window print survives can depend on
  larger prints later in the bucket; the WARNING makes that visible.

decision (``side_estimate``: frozen Phase 3.3.9 NBBO rule + §3.4 exemptions):
  Prints not priced against the contemporaneous NBBO are ``unknown``:
  ``sale_cond_codes`` in {contingent_trade, average_price_trade,
  prior_reference_price}, ``trade_code`` in {qualified_contingent_trade,
  derivative_priced}, or any non-null ``ext_hour_sold_codes``. Otherwise a
  missing, unparseable, non-finite, non-positive or crossed NBBO gives
  ``unknown``; ``price >= nbbo_ask`` gives ``above_ask``;
  ``price <= nbbo_bid`` gives ``at_or_below_bid``; anything else is
  ``midpoint``. Prices parse as ``Decimal`` from their string form.

decision (no data vs errors):
  ``UnusualWhalesNotFoundError`` (HTTP 404/422) maps to ``()``. Auth
  errors, 5xx after retries and an open breaker propagate (contract §3.9:
  a bad key must never look like "no data").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from uoa_detector.providers.dark_pool import DarkPoolPrint
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Transport bound, not a scoring threshold: the request page size, which is
# the largest page verified live on /api/darkpool/{ticker} (2026-09-14).
# A response of this many rows is a truncated page.
_PAGE_LIMIT = 500

# Fetch bucket width. Rows are requested per UTC hour of ``before``.
_BUCKET_WIDTH = timedelta(hours=1)

# Prints whose price is not set against the contemporaneous NBBO.
_NBBO_EXEMPT_SALE_CONDS = frozenset(
    {"contingent_trade", "average_price_trade", "prior_reference_price"},
)
_NBBO_EXEMPT_TRADE_CODES = frozenset(
    {"qualified_contingent_trade", "derivative_priced"},
)

SideEstimate = Literal["above_ask", "at_or_below_bid", "midpoint", "unknown"]


class UnusualWhalesDarkPoolProvider:
    """``DarkPoolPrintProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.dark_pool_seconds,
        )

    async def recent_prints(
        self,
        ticker: str,
        before: datetime,
        window: timedelta,
    ) -> Sequence[DarkPoolPrint]:
        """Return prints on ``ticker`` executed and reported within ``[before - window, before]``.

        A naive ``before`` is read as UTC.
        """
        symbol = ticker.upper()
        as_of = _as_utc(before)
        bucket = as_of.replace(minute=0, second=0, microsecond=0)
        rows = await self._cache.get_or_fetch(
            (symbol, bucket.isoformat(), window.total_seconds()),
            loader=lambda: self._fetch(symbol, as_of, bucket, window),
        )
        cutoff = as_of - window
        out: list[DarkPoolPrint] = []
        for row in rows:
            print_dto = _row_to_dark_pool_print(row, ticker=symbol, before=as_of)
            if print_dto is None:
                continue
            if print_dto.when < cutoff or print_dto.when > as_of:
                continue
            out.append(print_dto)
        return tuple(out)

    async def _fetch(
        self,
        symbol: str,
        as_of: datetime,
        bucket: datetime,
        window: timedelta,
    ) -> list[dict[str, Any]]:
        path = f"/api/darkpool/{symbol}"
        params: dict[str, Any] = {
            "date": as_of.astimezone(_ET).date().isoformat(),
            "newer_than": _format_iso_utc(bucket - window),
            "older_than": _format_iso_utc(bucket + _BUCKET_WIDTH),
            "limit": _PAGE_LIMIT,
            "order_by": "premium",
            "order": "desc",
        }
        try:
            resp = await self._client.request_json(path, params=params)
        except UnusualWhalesNotFoundError:
            return []
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        if len(data) >= _PAGE_LIMIT:
            _logger.warning(
                "UW dark pool page truncated: %s date=%s newer_than=%s "
                "older_than=%s returned %d rows (limit %d); premium-desc "
                "order keeps the largest prints",
                path,
                params["date"],
                params["newer_than"],
                params["older_than"],
                len(data),
                _PAGE_LIMIT,
            )
        return [d for d in data if isinstance(d, dict)]


def _row_to_dark_pool_print(
    row: dict[str, Any], *, ticker: str, before: datetime,
) -> DarkPoolPrint | None:
    """Parse one row; ``None`` when canceled, malformed or reported after ``before``."""
    if row.get("canceled") is True:
        return None
    try:
        when = _parse_iso_utc(str(row["executed_at"]))
        price = Decimal(str(row["price"]))
        size = int(row["size"])
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None
    if not price.is_finite():
        return None
    reported_raw = row.get("trf_executed_at")
    if reported_raw is not None:
        try:
            reported = _parse_iso_utc(str(reported_raw))
        except ValueError:
            return None
        if reported > before:
            return None
    return DarkPoolPrint(
        ticker=ticker,
        when=when,
        price=price,
        size=size,
        side_estimate=_side_estimate(row, price),
    )


def _side_estimate(row: dict[str, Any], price: Decimal) -> SideEstimate:
    """Classify a print against the NBBO at execution (contract §3.4)."""
    if (
        _code(row, "sale_cond_codes") in _NBBO_EXEMPT_SALE_CONDS
        or _code(row, "trade_code") in _NBBO_EXEMPT_TRADE_CODES
        or row.get("ext_hour_sold_codes") is not None
    ):
        return "unknown"
    bid = _finite_decimal(row.get("nbbo_bid"))
    ask = _finite_decimal(row.get("nbbo_ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return "unknown"
    if price >= ask:
        return "above_ask"
    if price <= bid:
        return "at_or_below_bid"
    return "midpoint"


def _code(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


def _finite_decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return None
    return value if value.is_finite() else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_iso_utc(value: datetime) -> str:
    # Whole seconds, flooring sub-second parts: an earlier ``newer_than``
    # only widens the request, and the client-side filter trims it.
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
