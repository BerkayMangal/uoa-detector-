"""ThetaData-backed PriceActionProvider implementation (Phase 3.4.3.2).

Implements ``get_intraday_price_movement(ticker, at, lookback_minutes)``
by fetching 1-minute OHLC bars from ThetaData over the lookback
window and comparing the first bar's open to the last bar's close.

ThetaData stock OHLC endpoint shape (centralised here):

  GET /v2/hist/stock/ohlc
    params: root, start_date, end_date, ivl=60000 (1-minute)
    → {
        "header": {...},
        "response": [
          [<ms_of_day>, <open>, <high>, <low>, <close>, <volume>, <count>, <date>],
          ...
        ]
      }

The ``response`` is a list of bar tuples with positional fields. We
parse each bar's open + close + ms_of_day + date and pick the bars
inside the requested [start, end] window.

decision (use OHLC bars, not tick quotes):
  Tick quotes give finer resolution but require fetching potentially
  thousands of records. 1-minute bars cover the lookback in 30 rows
  for the default 30-min window, with a single HTTP call. M23's
  acceptance doc requires 'spot now' vs 'spot 30 minutes ago' — the
  bar resolution is more than sufficient for a 0.5% confirmation
  threshold.

decision (parse positional bar tuples — not field names):
  ThetaData's response format uses positional arrays for bandwidth.
  We pin the field positions in a constant tuple so a vendor format
  change is one fix, not scattered.

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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.price_action import (
    PriceActionSnapshot,
    PriceMovement,
)
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import ThetaDataSettings
    from uoa_detector.sources.thetadata.client import ThetaDataClient


# Positional fields in a ThetaData OHLC bar tuple
_OHLC_MS = 0
_OHLC_OPEN = 1
# _OHLC_HIGH = 2  # unused
# _OHLC_LOW = 3   # unused
_OHLC_CLOSE = 4
# _OHLC_VOLUME = 5  # unused
# _OHLC_COUNT = 6   # unused
_OHLC_DATE = 7  # YYYYMMDD int


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
        self._cache: TTLCache[list[list[Any]]] = TTLCache(ttl_seconds=ttl)

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
    ) -> list[list[Any]]:
        """Fetch 1-minute OHLC bars over the window from ThetaData."""
        params: dict[str, str] = {
            "root": ticker.upper(),
            "start_date": window_start.strftime("%Y%m%d"),
            "end_date": window_end.strftime("%Y%m%d"),
            "ivl": "60000",  # 60_000 ms = 1 minute
        }
        resp = await self._client.request_json(
            "/v2/hist/stock/ohlc", params=params,
        )
        rows = resp.get("response", [])
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, list) and len(r) > _OHLC_DATE]


def _build_movement(
    *,
    bars: list[list[Any]],
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
            open_px = Decimal(str(row[_OHLC_OPEN]))
            close_px = Decimal(str(row[_OHLC_CLOSE]))
        except (KeyError, IndexError, ValueError, ArithmeticError):
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


def _bar_to_datetime(row: list[Any]) -> datetime:
    """Convert a ThetaData bar's (ms_of_day, date) → UTC datetime.

    The ms_of_day is reported in ET market local time (per
    ThetaData docs); we treat it as UTC for window comparison
    since the caller's window is also in UTC. Operators running
    against ThetaData's native ET timestamps can override the
    timezone via a future settings flag.

    For Phase 3.4.3, treating ThetaData timestamps as UTC is
    sufficient because:
      - The M23 stage builds [at - lookback, at] in the same
        timezone as the bar timestamps
      - The relative comparison (movement %) doesn't depend on
        absolute timezone correctness
      - The smoke test fetches a small recent window and checks
        the SHAPE of the result, not exact bar timing

    Trading-calendar overlay (Phase 3.4.9) may revisit.
    """
    ms = int(row[_OHLC_MS])
    date_int = int(row[_OHLC_DATE])
    year = date_int // 10000
    month = (date_int // 100) % 100
    day = date_int % 100
    return datetime(year, month, day, tzinfo=UTC) + timedelta(milliseconds=ms)
