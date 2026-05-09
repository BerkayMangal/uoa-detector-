"""ThetaData-backed PriceActionProvider implementation (Phase 3.4.3.2).

Implements ``get_intraday_price_movement(ticker, at, lookback_minutes)``
by fetching 1-minute OHLC bars from ThetaData over the lookback
window and comparing the first bar's open to the last bar's close.

Phase 3.3.7.3 (v2 → v3): switched from ``/v2/hist/stock/ohlc``
positional arrays to ``/v3/stock/history/ohlc`` named-dict
objects. Endpoint shape per
https://docs.thetadata.us/operations/stock_history_ohlc.html:

  GET /v3/stock/history/ohlc
    params: symbol (was 'root'), start_date, end_date,
            interval (was 'ivl', now string '1m'/'5m'/etc),
            format=json (auto-injected by ThetaDataClient)
    → [
        {"timestamp": "2024-01-02T09:30:00.000",
         "open": 192.50, "high": 193.20, "low": 192.30,
         "close": 193.10, "volume": 1234, "count": 56,
         "vwap": 192.85},
        ...
      ]

Each row is a named-dict object; we read by field name.

decision (use OHLC bars, not tick quotes):
  Tick quotes give finer resolution but require fetching potentially
  thousands of records. 1-minute bars cover the lookback in 30 rows
  for the default 30-min window, with a single HTTP call. M23's
  acceptance doc requires 'spot now' vs 'spot 30 minutes ago' — the
  bar resolution is more than sufficient for a 0.5% confirmation
  threshold.

decision (parse named-dict bars, not positional):
  Phase 3.3.2 used positional arrays (v2 wire). Phase 3.3.7.3
  switched to v3's named-dict response. Reading by field name is
  safer (no constants to keep in sync with vendor docs) and the
  small per-bar overhead is irrelevant at < 100 bars per call.

decision (fall back to None on missing bars):
  If the lookback window contains zero bars (after-hours timestamp,
  ticker not yet trading, holiday), return None. M23 treats None
  as the neutral_score fallback per acceptance doc.

decision (per-(ticker, lookback-bucket) cache key):
  Two M23 calls within the same lookback window for the same ticker
  share a cached fetch. Bucket = floor((at - lookback) to minute) +
  '|' + str(lookback_minutes). Coarser bucketing would risk staleness;
  finer would defeat the cache.

decision (no snapshot_at impl — Phase 3.4.3 only ships the
intraday-movement method):
  ``snapshot_at`` returns None for now; that surface is for future
  enrichment (VWAP, HH/HL pattern detection) which Phase 3.4.3
  doesn't need. The ThetaData provider implements only what M23
  requires.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.price_action import (
    PriceActionSnapshot,
    PriceMovement,
)
from uoa_detector.sources.thetadata.mapping import (
    iso_timestamp_to_utc_datetime,
)
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import ThetaDataSettings
    from uoa_detector.sources.thetadata.client import ThetaDataClient


# v3 OHLC bar dict field names (centralised so a vendor field rename
# is one edit, not scattered).
_OHLC_FIELD_TIMESTAMP = "timestamp"
_OHLC_FIELD_OPEN = "open"
_OHLC_FIELD_CLOSE = "close"


class ThetaDataPriceActionProvider:
    """``PriceActionProvider`` Protocol implementation backed by ThetaData."""

    def __init__(
        self,
        *,
        client: ThetaDataClient,
        settings: ThetaDataSettings,
    ) -> None:
        self._client = client
        self._settings = settings
        # Cache key: f"{ticker}|{date_iso}|{lookback_minutes}"
        ttl = getattr(settings, "intraday_cache_ttl_seconds", 60)
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(ttl_seconds=ttl)

    async def snapshot_at(
        self,
        ticker: str,
        at: datetime,
    ) -> PriceActionSnapshot | None:
        """Phase 3.4.3 doesn't implement snapshot_at; M23 doesn't need it."""
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
            loader=lambda: self._fetch(ticker, window_start, at),
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
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch 1-minute OHLC bars over the window from ThetaData v3."""
        params: dict[str, str] = {
            "symbol": ticker.upper(),
            "start_date": window_start.strftime("%Y%m%d"),
            "end_date": window_end.strftime("%Y%m%d"),
            "interval": "1m",
        }
        resp = await self._client.request_json(
            "/v3/stock/history/ohlc", params=params,
        )
        # v3: response is a top-level array of dicts.
        # Defensive fallback: if v2-style {header, response: [...]}
        # still appears (e.g., older Terminal not yet upgraded), pull
        # the inner list. Anything else → empty.
        if isinstance(resp, list):
            return [r for r in resp if isinstance(r, dict)]
        if isinstance(resp, dict):
            inner = resp.get("response", [])
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        return []


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
            ts = _bar_to_datetime(row)
            open_px = Decimal(str(row[_OHLC_FIELD_OPEN]))
            close_px = Decimal(str(row[_OHLC_FIELD_CLOSE]))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if window_start <= ts <= window_end:
            in_window.append((ts, open_px, close_px))

    if not in_window:
        return None

    in_window.sort(key=lambda x: x[0])
    spot_lookback_ago = in_window[0][1]   # earliest bar's open
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


def _bar_to_datetime(row: dict[str, Any]) -> datetime:
    """Parse a v3 OHLC bar's ``timestamp`` ISO string → UTC datetime.

    Phase 3.3.7.3: v3 bars carry ISO timestamps (``YYYY-MM-DDTHH:mm:ss.SSS``).
    Treated as ET-naive per ThetaData convention (centralised in
    ``iso_timestamp_to_utc_datetime``).

    The M23 stage builds its [at - lookback, at] window in UTC; the
    converter here returns UTC; comparison happens in UTC. Trading-
    calendar overlay (Phase 3.5+) may revisit boundary handling for
    half-days.
    """
    raw = row[_OHLC_FIELD_TIMESTAMP]
    if not isinstance(raw, str):
        msg = (
            f"_bar_to_datetime: '{_OHLC_FIELD_TIMESTAMP}' must be a "
            f"string (got {type(raw).__name__})"
        )
        raise ValueError(msg)
    return iso_timestamp_to_utc_datetime(raw)
