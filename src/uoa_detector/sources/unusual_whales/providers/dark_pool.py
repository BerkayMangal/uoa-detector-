"""Unusual Whales DarkPoolPrintProvider implementation.

Phase 3.3.3.4: implements ``DarkPoolPrintProvider`` Protocol backed
by UW's dark-pool-prints endpoint. UW exposes recent off-exchange
(TRF) prints per ticker; we filter to the requested time window
and return them as a sequence of ``DarkPoolPrint`` DTOs.

The provider is pure data-shipping. Module 26 (Dark Pool
Confirmation) consumes the prints and applies its own logic
(prints below bid → bullish absorption; above ask → bearish
distribution; mid-price + drift → confirmation).

UW endpoint shape:

  GET /api/darkpool/{ticker}/prints?after={iso}
    → {
        "data": [
          {
            "executed_at": "2024-01-15T14:25:30Z",
            "price": "150.42",
            "size": 50000,
            "side_estimate": "midpoint"
          },
          ...
        ]
      }

decision (cache key includes ticker only, time-window filtering at consumer):
  UW's endpoint returns recent prints (the ``after`` param bounds
  the lookback). We cache the full response per ticker; the
  consumer (``recent_prints``) filters to the requested window
  in-memory. TTL=60s default keeps prints fresh enough that the
  Module 26 stage sees near-real-time dark-pool flow.

decision (UW side_estimate values pass through directly):
  UW publishes 'above_ask' / 'at_or_below_bid' / 'midpoint' /
  'unknown' which already match our DarkPoolPrint Literal. No
  transformation needed beyond a defensive default.

decision (cap returned prints at 1000 per call):
  UW may return very large windows; downstream code shouldn't
  deal with arbitrary-size sequences. 1000 prints is well above
  what Module 26 examines (typically last 30-60 minutes), with
  margin for very heavy-volume tickers.
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
        path = f"/api/darkpool/{ticker.upper()}/prints"
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
    side_raw = str(row.get("side_estimate", "unknown")).lower()
    if side_raw not in ("above_ask", "at_or_below_bid", "midpoint", "unknown"):
        side_raw = "unknown"
    return DarkPoolPrint(
        ticker=ticker.upper(),
        when=when,
        price=price,
        size=size,
        side_estimate=side_raw,
    )


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
