"""Unusual Whales-backed PriceActionProvider implementation (Phase 3.3.8.2).

Replaces ``ThetaDataPriceActionProvider`` as M23's default backend.
Rationale and history: see ``docs/phase-3.3.8-acceptance.md`` —
ThetaData's stock OHLC endpoint requires a STOCK.VALUE subscription
add-on which operators on OPTION.STANDARD do not have; UW exposes
stock OHLC bars under the same API-Plus subscription that already
serves the other Phase 3.3.3 providers, so the Phase 3.5.1 smoke
contract can be satisfied without a second subscription.

The ThetaData implementation is retained in
``uoa_detector.sources.thetadata.providers.price_action`` for
operators that DO have STOCK.VALUE.

UW endpoint shape (verified Phase 3.3.8.2 against api.unusualwhales.com
docs at ``/docs/operations/PublicApi.TickerController.ohlc``):

  GET /api/stock/{ticker}/ohlc/{candle_size}
    candle_size enum: 1m | 5m | 10m | 15m | 30m | 1h | 4h | 1d | 1w
    optional params:
      timeframe   — range string (YTD, 1D/2D, 1W/2W, 1M/2M, 1Y/2Y)
      end_date    — trading date YYYY-MM-DD
      date        — single trading date YYYY-MM-DD
      limit       — 1..2500
    → {
        "data": [
          {
            "open": 192.50, "high": 193.20, "low": 192.30,
            "close": 193.10, "volume": 1234, "total_volume": ...,
            "start_time": "2024-01-02T09:30:00.000-05:00",
            "end_time":   "2024-01-02T09:31:00.000-05:00",
            "market_time": "po"
          },
          ...
        ]
      }

decision (use 1m bars, not 5m):
  M23's default confirmation window is 30 minutes; 1m bars give
  the stage 30 rows over the window, plenty of granularity for a
  0.5% confirmation threshold. Per-call cost is one HTTP request
  per (ticker, date) — TTL cache absorbs same-window duplicates.

decision (request the full session via ``date``, not just the
window):
  UW returns up to ``limit`` bars per call. Requesting the full
  trading date and filtering window in-process is simpler than
  computing per-request start_time / end_time. The session has
  ≤ 390 1m bars (RTH), well below the 2500 cap. The TTL cache
  keys on (ticker, date_iso, lookback_minutes); two M23 calls in
  the same session for the same ticker share one HTTP request.

decision (parse ISO timestamps, treat as UTC if tz-aware,
otherwise as ET-naive):
  UW timestamps in observed responses carry a UTC offset (e.g.
  ``-05:00`` for EST). When the offset is present we parse and
  normalize to UTC. If a future schema change drops the offset,
  we treat it as ET-naive (UW publishes ET sessions) and convert
  through ``zoneinfo``. The conversion code branches on
  ``parsed.tzinfo is None``.

decision (return None on empty data, malformed rows, or non-list
shape):
  Same contract as the ThetaData implementation: M23 maps None to
  the neutral fallback per acceptance doc edge case. Subscription
  / auth failures surface as raised exceptions from the client
  layer; M23's Phase 3.3.8.1 graceful-degradation handler maps
  those to the same neutral fallback under branch=provider_error.

decision (no ``snapshot_at`` implementation):
  M23 doesn't need it; ThetaData provider stubs it the same way.
  Returns None — future PriceAction consumers (VWAP, HH/HL) can
  add the implementation when needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from uoa_detector.providers.price_action import (
    PriceActionSnapshot,
    PriceMovement,
)
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_ET = ZoneInfo("America/New_York")

# Default candle size (string, matches UW path parameter)
_CANDLE_SIZE_1M = "1m"


class UnusualWhalesPriceActionProvider:
    """``PriceActionProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
        candle_size: str = _CANDLE_SIZE_1M,
    ) -> None:
        self._client = client
        self._candle_size = candle_size
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.intraday_price_seconds,
        )

    async def snapshot_at(
        self,
        ticker: str,
        at: datetime,
    ) -> PriceActionSnapshot | None:
        """Phase 3.3.8.2 doesn't implement snapshot_at; M23 doesn't need it."""
        del ticker, at
        return None

    async def get_intraday_price_movement(
        self,
        ticker: str,
        at: datetime,
        lookback_minutes: int,
    ) -> PriceMovement | None:
        """Return signed spot movement over [at - lookback, at]."""
        if lookback_minutes <= 0:
            return None
        window_start = at - timedelta(minutes=lookback_minutes)
        cache_key = (
            f"{ticker.upper()}|{at.date().isoformat()}|{lookback_minutes}"
        )
        bars = await self._cache.get_or_fetch(
            cache_key,
            loader=lambda: self._fetch(ticker, at),
        )
        return _build_movement(
            bars=bars,
            ticker=ticker.upper(),
            window_start=window_start,
            window_end=at,
            lookback_minutes=lookback_minutes,
        )

    async def _fetch(
        self,
        ticker: str,
        at: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch 1-minute OHLC bars for the trading date containing ``at``."""
        # UW expects ET trading date in YYYY-MM-DD; convert ``at`` (UTC) to ET.
        et_date = at.astimezone(_ET).date()
        path = f"/api/stock/{ticker.upper()}/ohlc/{self._candle_size}"
        params: dict[str, Any] = {"date": et_date.isoformat()}
        resp = await self._client.request_json(path, params=params)
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def _build_movement(
    *,
    bars: list[dict[str, Any]],
    ticker: str,
    window_start: datetime,
    window_end: datetime,
    lookback_minutes: int,
) -> PriceMovement | None:
    """Pick first/last bars in window; compute movement.

    Pure function — directly unit-testable.
    """
    if not bars:
        return None

    in_window: list[tuple[datetime, Decimal, Decimal]] = []
    for row in bars:
        try:
            ts = _parse_bar_timestamp(row)
            open_px = Decimal(str(row["open"]))
            close_px = Decimal(str(row["close"]))
        except (KeyError, ValueError, ArithmeticError, TypeError):
            continue
        if window_start <= ts <= window_end:
            in_window.append((ts, open_px, close_px))

    if not in_window:
        return None

    in_window.sort(key=lambda x: x[0])
    spot_lookback_ago = in_window[0][1]  # earliest bar's open
    spot_at = in_window[-1][2]            # latest bar's close
    if spot_lookback_ago == Decimal("0"):
        return None  # avoid division by zero on a degenerate bar
    move_pct = float(
        (spot_at - spot_lookback_ago) / spot_lookback_ago,
    )
    return PriceMovement(
        ticker=ticker,
        as_of=window_end,
        spot_at=spot_at,
        spot_lookback_ago=spot_lookback_ago,
        move_pct=move_pct,
        lookback_minutes_actual=lookback_minutes,
    )


def _parse_bar_timestamp(row: dict[str, Any]) -> datetime:
    """Parse a UW OHLC bar's ``start_time`` ISO string → UTC datetime.

    UW publishes timestamps with an explicit offset (e.g.
    ``2024-01-02T09:30:00.000-05:00``). If a future schema change
    drops the offset, we fall back to ET-naive interpretation.
    """
    raw = row["start_time"]
    if not isinstance(raw, str):
        msg = f"_parse_bar_timestamp: 'start_time' must be a string (got {type(raw).__name__})"
        raise ValueError(msg)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        # Naive → assume ET, convert to UTC
        parsed = parsed.replace(tzinfo=_ET)
    return parsed.astimezone(UTC)
