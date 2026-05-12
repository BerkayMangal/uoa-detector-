"""Unusual Whales DarkPoolPrintProvider implementation.

Phase 3.3.3.4: implements ``DarkPoolPrintProvider`` Protocol backed
by UW's dark-pool-prints endpoint. UW exposes recent off-exchange
(TRF) prints per ticker; we filter to the requested time window
and return them as a sequence of ``DarkPoolPrint`` DTOs.

The provider is pure data-shipping. Module 26 (Dark Pool
Confirmation) consumes the prints and applies its own logic
(prints below bid → bullish absorption; above ask → bearish
distribution; mid-price + drift → confirmation).

UW endpoint shape (Phase 3.3.9.3 — migrated from
``/api/darkpool/{ticker}/prints``, which UW deprecated; current
path drops the ``/prints`` suffix and ships an enriched row schema
that no longer includes a precomputed ``side_estimate``):

  GET /api/darkpool/{ticker}
    → {
        "data": [
          {
            "executed_at": "2026-05-11T23:43:15Z",
            "price": "292.6001",
            "size": 2000,
            "premium": "585200.20",
            "nbbo_bid": "292.6",
            "nbbo_ask": "292.67",
            "nbbo_bid_quantity": 116,
            "nbbo_ask_quantity": 48,
            "market_center": "L",
            "trade_settlement": "regular",
            "sale_cond_codes": null,
            "ext_hour_sold_codes": "extended_hours_trade",
            "tracking_id": 26946710210168
          },
          ...
        ]
      }

decision (Phase 3.3.9.3 — derive ``side_estimate`` locally):
  UW dropped the precomputed ``side_estimate``. We derive it from
  the print price against NBBO at execution time:
    price ≥ nbbo_ask                → "above_ask"
    price ≤ nbbo_bid                → "at_or_below_bid"
    nbbo_bid < price < nbbo_ask     → "midpoint"
    nbbo_bid / nbbo_ask missing or non-positive → "unknown"
  This matches the Phase 3.4.6 M26 stage's expectation, which
  reads the Literal field without further inference. The legacy
  ``side_estimate`` field is still tolerated in fixtures for
  Phase 3.3.3 unit-test back-compat.

decision (cache key includes ticker only, time-window filtering at consumer):
  Unchanged from Phase 3.3.3.

decision (cap returned prints at 1000 per call):
  Unchanged from Phase 3.3.3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.dark_pool import DarkPoolPrint
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_MAX_PRINTS_PER_RESPONSE = 1000


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
        """Return DP prints on ``ticker`` within ``window`` before ``before``."""
        rows = await self._cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch(ticker, before),
        )
        cutoff = before - window
        out: list[DarkPoolPrint] = []
        for row in rows:
            print_dto = _row_to_dark_pool_print(row, ticker=ticker)
            if print_dto is None:
                continue
            if print_dto.when < cutoff or print_dto.when > before:
                continue
            out.append(print_dto)
        return tuple(out)

    async def _fetch(
        self, ticker: str, before: datetime,
    ) -> list[dict[str, Any]]:
        # Pull a generous lookback (60 min). The consumer filters down.
        after = (before - timedelta(minutes=60)).isoformat()
        params = {"after": after}
        path = f"/api/darkpool/{ticker.upper()}"
        resp = await self._client.request_json(path, params=params)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        rows = [d for d in data if isinstance(d, dict)]
        return rows[:_MAX_PRINTS_PER_RESPONSE]


def _row_to_dark_pool_print(
    row: dict[str, Any], *, ticker: str,
) -> DarkPoolPrint | None:
    try:
        when = _parse_iso_utc(str(row["executed_at"]))
        price = Decimal(str(row["price"]))
        size = int(row["size"])
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None
    side_raw = _row_side_estimate(row, price)
    return DarkPoolPrint(
        ticker=ticker.upper(),
        when=when,
        price=price,
        size=size,
        side_estimate=side_raw,
    )


def _row_side_estimate(row: dict[str, Any], price: Decimal) -> str:
    """Phase 3.3.9.3: derive side_estimate from NBBO when absent.

    Legacy ``side_estimate`` field (Phase 3.3.3 fixture compat) takes
    precedence if present and valid.
    """
    legacy = row.get("side_estimate")
    if isinstance(legacy, str):
        v = legacy.lower()
        if v in ("above_ask", "at_or_below_bid", "midpoint", "unknown"):
            return v
    bid_raw = row.get("nbbo_bid")
    ask_raw = row.get("nbbo_ask")
    if bid_raw is None or ask_raw is None:
        return "unknown"
    try:
        bid = Decimal(str(bid_raw))
        ask = Decimal(str(ask_raw))
    except (ValueError, ArithmeticError):
        return "unknown"
    if bid <= Decimal("0") or ask <= Decimal("0") or ask < bid:
        return "unknown"
    if price >= ask:
        return "above_ask"
    if price <= bid:
        return "at_or_below_bid"
    return "midpoint"


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
