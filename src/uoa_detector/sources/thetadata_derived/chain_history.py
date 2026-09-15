"""Per-ticker chain history for the self-derived IV + OI axes (Phase 3.6.4).

Reads the same daily chain-snapshot parquet the GEX provider uses
(``scripts/download_chain_snapshots.py``) but indexes it for two
point-in-time lookups the M24 (IV regime) and M27 (OI delta) providers
need:

  - per (ticker, contract) the OI + IV time series (sorted by day), so
    "OI as of ``when``" and "OI the day after ``trade_date``" are O(log n);
  - per ticker the daily **ATM IV** series (the IV of the strike nearest
    that day's spot), so an IV rank — current ATM IV vs its trailing range
    — can be computed as of any instant.

As-of selection is the most-recent snapshot day ≤ the query day (the
look-ahead guard); ``next_day`` is the first day strictly after.

The snapshot window is ~250 trading days, so the IV rank uses an expanding
trailing window capped at ``lookback`` days; it returns None until at least
``_MIN_RANK_DAYS`` of history exist.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pyarrow.parquet as pq

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

_logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK = 252
_MIN_RANK_DAYS = 20

_ContractKey = tuple[Decimal, date, str]


@dataclass(frozen=True)
class _TickerHistory:
    # contract -> (sorted dates, parallel oi list, parallel iv list)
    contracts: dict[_ContractKey, tuple[list[date], list[int], list[float]]]
    # ATM IV series
    atm_dates: list[date]
    atm_iv: list[float]


class ChainHistory:
    """Per-ticker OI/IV history loaded lazily from the snapshot parquet."""

    def __init__(self, snapshots_dir: Path) -> None:
        self._dir = snapshots_dir
        self._cache: dict[str, _TickerHistory | None] = {}

    # -- OI lookups (M27) ------------------------------------------------

    def oi_at(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: str, when: datetime,
    ) -> tuple[date, int] | None:
        """OI of the contract as of the most-recent snapshot day ≤ when."""
        hist = self._hist(ticker)
        if hist is None:
            return None
        series = hist.contracts.get((strike, expiry, option_type))
        if series is None:
            return None
        dates, ois, _ = series
        pos = bisect.bisect_right(dates, when.date()) - 1
        if pos < 0:
            return None
        return dates[pos], ois[pos]

    def oi_next_day(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: str, trade_date: date,
    ) -> tuple[date, int] | None:
        """OI of the contract on the first snapshot day strictly after trade_date."""
        hist = self._hist(ticker)
        if hist is None:
            return None
        series = hist.contracts.get((strike, expiry, option_type))
        if series is None:
            return None
        dates, ois, _ = series
        pos = bisect.bisect_right(dates, trade_date)
        if pos >= len(dates):
            return None
        return dates[pos], ois[pos]

    # -- IV lookups (M24) ------------------------------------------------

    def iv_at(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: str, when: datetime,
    ) -> tuple[date, float] | None:
        """Contract IV as of the most-recent snapshot day ≤ when."""
        hist = self._hist(ticker)
        if hist is None:
            return None
        series = hist.contracts.get((strike, expiry, option_type))
        if series is None:
            return None
        dates, _, ivs = series
        pos = bisect.bisect_right(dates, when.date()) - 1
        if pos < 0:
            return None
        return dates[pos], ivs[pos]

    def atm_iv_rank(
        self, ticker: str, when: datetime, lookback: int = _DEFAULT_LOOKBACK,
    ) -> float | None:
        """ATM IV rank (0..100) as of ``when``: current ATM IV vs the
        trailing [≤lookback]-day ATM IV range. None until enough history."""
        hist = self._hist(ticker)
        if hist is None:
            return None
        pos = bisect.bisect_right(hist.atm_dates, when.date()) - 1
        if pos < 0:
            return None
        lo = max(0, pos - lookback + 1)
        window = hist.atm_iv[lo:pos + 1]
        if len(window) < _MIN_RANK_DAYS:
            return None
        wmin, wmax = min(window), max(window)
        if wmax <= wmin:
            return None
        current = hist.atm_iv[pos]
        return (current - wmin) / (wmax - wmin) * 100.0

    # -- loading ---------------------------------------------------------

    def _hist(self, ticker: str) -> _TickerHistory | None:
        if ticker not in self._cache:
            self._cache[ticker] = self._load(ticker)
        return self._cache[ticker]

    def _load(self, ticker: str) -> _TickerHistory | None:
        path = self._dir / f"{ticker.upper()}.parquet"
        if not path.exists():
            _logger.warning("ChainHistory: no snapshot file for %s", ticker)
            return None
        table = pq.read_table(path)  # type: ignore[no-untyped-call]
        rows = table.to_pylist()
        raw: dict[_ContractKey, list[tuple[date, int, float]]] = {}
        # ATM pick: per day, the contract with strike nearest spot.
        atm_best: dict[date, tuple[float, float]] = {}  # day -> (dist, iv)
        for r in rows:
            d = _as_date(r["snapshot_date"])
            strike = Decimal(str(r["strike"]))
            key: _ContractKey = (strike, _as_date(r["expiry"]), r["option_type"])
            oi = int(r["open_interest"])
            iv = float(r["implied_volatility"])
            raw.setdefault(key, []).append((d, oi, iv))
            spot = float(r["spot"])
            dist = abs(float(strike) - spot)
            prev = atm_best.get(d)
            if prev is None or dist < prev[0]:
                atm_best[d] = (dist, iv)
        if not raw:
            return None
        contracts: dict[_ContractKey, tuple[list[date], list[int], list[float]]] = {}
        for key, pts in raw.items():
            pts.sort(key=lambda p: p[0])
            contracts[key] = (
                [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts],
            )
        atm_dates = sorted(atm_best)
        atm_iv = [atm_best[d][1] for d in atm_dates]
        return _TickerHistory(contracts=contracts, atm_dates=atm_dates, atm_iv=atm_iv)


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return date(value.year, value.month, value.day)
    msg = f"expected a date-like cell, got {value!r}"
    raise TypeError(msg)


# Re-exported for the provider modules' type hints.
OptionType = Literal["call", "put"]
