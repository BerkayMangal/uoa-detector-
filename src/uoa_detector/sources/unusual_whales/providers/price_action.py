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

UW endpoint shape (live-verified 2026-09-14, Phase 3.9.11, against
``GET /api/stock/AAPL/ohlc/1m?date=2026-09-11``):

  GET /api/stock/{ticker}/ohlc/{candle_size}
    candle_size enum: 1m | 5m | 10m | 15m | 30m | 1h | 4h | 1d | 1w
    params used: date — ET trading date YYYY-MM-DD
    → {
        "data": [
          {
            "open": "332.5898", "high": "332.5898", "low": "332.56",
            "close": "332.58", "volume": 2189, "total_volume": 50716811,
            "start_time": "2026-09-11T23:59:00Z",
            "end_time":   "2026-09-12T00:00:00Z",
            "market_time": "po"
          },
          ...
        ]
      }

  Live facts the parser depends on:
    - prices are strings (``Decimal(str(...))`` also accepts numbers);
    - ``start_time`` / ``end_time`` are UTC ISO strings with a ``Z``
      suffix, and ``end_time - start_time`` equals the candle size
      (809/809 1m rows were exactly 60 s);
    - rows are NEWEST FIRST;
    - the day payload covers the whole extended session, 08:00-23:59 UTC
      (809 1m bars on 2026-09-11: market_time ``pr`` 246, ``r`` 390,
      ``po`` 173). No ``market_time`` filter is applied here.

decision (use 1m bars, not 5m):
  M23's default confirmation window is 30 minutes; 1m bars give
  the stage 30 rows over the window, plenty of granularity for a
  0.5% confirmation threshold.

decision (request the full session via ``date``, not just the
window):
  One request returns the whole ET trading day (~800 1m bars, below
  the 2500 row cap). Requesting the full day and filtering the window
  in-process is simpler than computing per-request bounds, and it lets
  every M23 call for the same ticker and day share one HTTP request.

decision (cache key = (ticker, ET date of ``at``, candle_size), Phase
3.9.11):
  The payload is the whole ET trading day, so the lookback is not part
  of the key: two lookbacks on the same day share one fetch. The date
  is the ET date — the same value sent as the ``date`` param — computed
  once per call so key and request cannot drift apart. Before 3.9.11
  the key used the UTC date and the lookback, so an event after 20:00
  ET could be served the next UTC day's cached bars.

decision (no look-ahead: completed bars only, Phase 3.9.11):
  A bar's ``close`` is knowable only when the bar ends. A bar is usable
  for ``at`` only if it started at or after ``at - lookback`` and ended
  at or before ``at``. ``spot_at`` is the close of the latest usable
  bar; ``spot_lookback_ago`` is the open of the earliest usable bar.
  Before 3.9.11 a bar that started at or before ``at`` was used even
  though its close printed up to one candle after ``at``. The bar end
  is the parsed ``end_time`` when it is present and later than
  ``start_time``; otherwise ``start_time`` plus the candle duration.
  In live mode the newest candle is still forming, so it is always
  excluded; the TTL cache can make ``spot_at`` stale, never future.

decision (candle_size restricted to intraday sizes):
  The completed-bar rule needs a candle duration for the fallback bar
  end. ``1d`` / ``1w`` have no fixed intraday duration, so the
  constructor rejects them with ``ValueError``.

decision (parse ISO timestamps, normalise to UTC; naive → ET):
  Live timestamps carry ``Z``. If a future schema change drops the
  offset, a naive timestamp is read as ET (UW publishes ET sessions)
  and converted through ``zoneinfo``.

decision (return None on empty data, malformed rows, non-list shape,
or no usable bar):
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

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
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

# Duration of each intraday ``candle_size`` path value UW accepts. This
# is the vendor enum's unit definition (used as the bar-end fallback when
# ``end_time`` is absent), not a scoring threshold (D8).
_INTRADAY_CANDLE_DURATIONS: Final[dict[str, timedelta]] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "10m": timedelta(minutes=10),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


class UnusualWhalesPriceActionProvider:
    """``PriceActionProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
        candle_size: str = _CANDLE_SIZE_1M,
    ) -> None:
        duration = _INTRADAY_CANDLE_DURATIONS.get(candle_size)
        if duration is None:
            msg = (
                "UnusualWhalesPriceActionProvider: candle_size must be one of "
                f"{sorted(_INTRADAY_CANDLE_DURATIONS)} (got {candle_size!r})"
            )
            raise ValueError(msg)
        self._client = client
        self._candle_size = candle_size
        self._candle_duration = duration
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
        """Return signed spot movement over completed bars in [at - lookback, at]."""
        if lookback_minutes <= 0:
            return None
        symbol = ticker.upper()
        et_date = at.astimezone(_ET).date()
        cache_key = (symbol, et_date.isoformat(), self._candle_size)
        bars = await self._cache.get_or_fetch(
            cache_key,
            loader=lambda: self._fetch(symbol, et_date),
        )
        return _build_movement(
            bars=bars,
            ticker=symbol,
            window_start=at - timedelta(minutes=lookback_minutes),
            window_end=at,
            lookback_minutes=lookback_minutes,
            candle_duration=self._candle_duration,
        )

    async def _fetch(
        self,
        symbol: str,
        et_date: date,
    ) -> list[dict[str, Any]]:
        """Fetch the OHLC bars for one ET trading date."""
        path = f"/api/stock/{symbol}/ohlc/{self._candle_size}"
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
    candle_duration: timedelta,
) -> PriceMovement | None:
    """Pick the first/last COMPLETED bars in the window; compute movement.

    A bar is usable only if ``start >= window_start`` and
    ``end <= window_end``. Pure function — directly unit-testable.
    """
    if not bars:
        return None

    usable: list[tuple[datetime, Decimal, Decimal]] = []
    for row in bars:
        try:
            start = _parse_bar_timestamp(row, "start_time")
            open_px = Decimal(str(row["open"]))
            close_px = Decimal(str(row["close"]))
        except (KeyError, ValueError, ArithmeticError, TypeError):
            continue
        end = _bar_end(row, start=start, candle_duration=candle_duration)
        if start >= window_start and end <= window_end:
            usable.append((start, open_px, close_px))

    if not usable:
        return None

    usable.sort(key=lambda x: x[0])
    spot_lookback_ago = usable[0][1]  # earliest completed bar's open
    spot_at = usable[-1][2]            # latest completed bar's close
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


def _bar_end(
    row: dict[str, Any],
    *,
    start: datetime,
    candle_duration: timedelta,
) -> datetime:
    """Return the UTC instant a bar's ``close`` became knowable.

    Uses the parsed ``end_time`` when it is present and later than
    ``start``. A missing, unparseable, or non-positive-length
    ``end_time`` falls back to ``start + candle_duration``, so a
    degenerate vendor value can never admit a bar that was still
    forming at ``at``.
    """
    try:
        end = _parse_bar_timestamp(row, "end_time")
    except (KeyError, ValueError, TypeError):
        return start + candle_duration
    if end <= start:
        return start + candle_duration
    return end


def _parse_bar_timestamp(row: dict[str, Any], field: str) -> datetime:
    """Parse a UW OHLC bar's ISO timestamp ``field`` → UTC datetime.

    Live UW timestamps carry an explicit ``Z`` (UTC). If a future
    schema change drops the offset, we fall back to ET-naive
    interpretation.
    """
    raw = row[field]
    if not isinstance(raw, str):
        msg = (
            f"_parse_bar_timestamp: {field!r} must be a string "
            f"(got {type(raw).__name__})"
        )
        raise ValueError(msg)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        # Naive → assume ET, convert to UTC
        parsed = parsed.replace(tzinfo=_ET)
    return parsed.astimezone(UTC)
