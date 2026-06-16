"""Self-derived price-action provider (Phase 3.6.5) for Module 23.

Reads the per-ticker minute-bar spot series built by
``scripts/compute_spot_series.py`` and serves the
``get_intraday_price_movement`` the M23 price-confirmation stage consumes —
spot at the print vs spot a lookback-window earlier — so M23's
"did spot confirm the flow direction" axis runs without Unusual Whales.

Guards: a quote is used only if it is within ``_MAX_STALENESS_MIN`` of the
query, and the lookback quote must be the SAME session day (no overnight
gap masquerading as an intraday move). Otherwise the movement is None and
M23 takes its neutral branch.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from uoa_detector.providers.price_action import (
    PriceActionSnapshot,
    PriceMovement,
)

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger(__name__)

_MAX_STALENESS_MIN = 30


@dataclass(frozen=True)
class _TickerBars:
    minutes: list[datetime]
    spots: list[float]


class SpotSeries:
    """Per-ticker minute-bar spot, loaded lazily from the precompute."""

    def __init__(self, series_dir: Path) -> None:
        self._dir = series_dir
        self._cache: dict[str, _TickerBars | None] = {}

    def spot_at(self, ticker: str, when: datetime) -> tuple[datetime, float] | None:
        """Most-recent minute bar ≤ when, if fresh (≤ _MAX_STALENESS_MIN old)."""
        bars = self._bars(ticker)
        if bars is None:
            return None
        pos = bisect.bisect_right(bars.minutes, when) - 1
        if pos < 0:
            return None
        bar_ts = bars.minutes[pos]
        if when - bar_ts > timedelta(minutes=_MAX_STALENESS_MIN):
            return None
        return bar_ts, bars.spots[pos]

    def _bars(self, ticker: str) -> _TickerBars | None:
        if ticker not in self._cache:
            self._cache[ticker] = self._load(ticker)
        return self._cache[ticker]

    def _load(self, ticker: str) -> _TickerBars | None:
        path = self._dir / f"{ticker.upper()}.parquet"
        if not path.exists():
            _logger.warning("SpotSeries: no spot file for %s", ticker)
            return None
        table = pq.read_table(path)  # type: ignore[no-untyped-call]
        minutes = table.column("minute").to_pylist()
        spots = [float(s) for s in table.column("spot").to_pylist()]
        if not minutes:
            return None
        return _TickerBars(minutes=minutes, spots=spots)


class ThetaDataPriceActionProvider:
    """``PriceActionProvider`` backed by the minute-bar spot series."""

    def __init__(self, series: SpotSeries) -> None:
        self._s = series

    async def snapshot_at(
        self, ticker: str, at: datetime,
    ) -> PriceActionSnapshot | None:
        res = self._s.spot_at(ticker, at)
        if res is None:
            return None
        _, spot = res
        spot_d = Decimal(str(spot))
        return PriceActionSnapshot(
            ticker=ticker, as_of=at, spot=spot_d, vwap=spot_d,
            direction="neutral",
        )

    async def get_intraday_price_movement(
        self, ticker: str, at: datetime, lookback_minutes: int,
    ) -> PriceMovement | None:
        now = self._s.spot_at(ticker, at)
        if now is None:
            return None
        then = self._s.spot_at(ticker, at - timedelta(minutes=lookback_minutes))
        if then is None:
            return None
        t_now, spot_now = now
        t_then, spot_then = then
        # No overnight gap masquerading as an intraday move.
        if t_now.date() != t_then.date() or spot_then <= 0.0:
            return None
        move_pct = (spot_now - spot_then) / spot_then * 100.0
        actual = max(0, int((t_now - t_then).total_seconds() // 60))
        return PriceMovement(
            ticker=ticker, as_of=at,
            spot_at=Decimal(str(spot_now)),
            spot_lookback_ago=Decimal(str(spot_then)),
            move_pct=move_pct,
            lookback_minutes_actual=actual,
        )
