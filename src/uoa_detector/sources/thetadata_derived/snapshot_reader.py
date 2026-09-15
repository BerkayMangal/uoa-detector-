"""Daily chain-snapshot reader (Phase 3.6.2).

Loads the per-ticker daily chain snapshots produced by
``scripts/compute_chain_snapshots.py`` and serves them through the
``ChainSnapshotSource`` interface the GEX provider consumes.

The snapshot parquet (one per ticker, ``{dir}/{TICKER}.parquet``) has one
row per (snapshot_date, contract):

  snapshot_date : date32     — the trading day the row is in force for
  strike        : float64
  expiry        : date32
  option_type   : string     — "call" | "put"
  open_interest : int64      — last OI seen that day for the contract
  implied_volatility : float64 — last IV seen that day
  spot          : float64     — last parity spot of that ticker-day

``as_of(ticker, at)`` returns the chain for the most-recent snapshot date
on or before ``at.date()`` — never a future day. That bound is the
look-ahead guard.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from uoa_detector.sources.thetadata_derived.chain import (
    ChainAsOf,
    ChainContract,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TickerIndex:
    """In-memory per-ticker index: sorted dates + per-day chain."""

    dates: list[date]
    by_date: dict[date, tuple[Decimal, tuple[ChainContract, ...]]]


class DailyChainSnapshotSource:
    """``ChainSnapshotSource`` backed by per-ticker daily-snapshot parquet.

    Each ticker's file is read once on first use and indexed in memory.
    """

    def __init__(self, snapshots_dir: Path) -> None:
        self._dir = snapshots_dir
        self._cache: dict[str, _TickerIndex | None] = {}

    def as_of(self, ticker: str, at: datetime) -> ChainAsOf | None:
        index = self._index_for(ticker)
        if index is None:
            return None
        day = at.date()
        # most-recent snapshot_date <= day
        pos = bisect.bisect_right(index.dates, day) - 1
        if pos < 0:
            return None
        snap_date = index.dates[pos]
        spot, contracts = index.by_date[snap_date]
        return ChainAsOf(
            ticker=ticker,
            snapshot_date=snap_date,
            spot=spot,
            contracts=contracts,
        )

    def _index_for(self, ticker: str) -> _TickerIndex | None:
        if ticker not in self._cache:
            self._cache[ticker] = self._load(ticker)
        return self._cache[ticker]

    def _load(self, ticker: str) -> _TickerIndex | None:
        path = self._dir / f"{ticker.upper()}.parquet"
        if not path.exists():
            _logger.warning(
                "DailyChainSnapshotSource: no snapshot file for %s (%s)",
                ticker, path,
            )
            return None
        table = pq.read_table(path)  # type: ignore[no-untyped-call]
        rows = table.to_pylist()
        by_date: dict[date, list[ChainContract]] = {}
        spot_by_date: dict[date, Decimal] = {}
        for r in rows:
            d = _as_date(r["snapshot_date"])
            by_date.setdefault(d, []).append(
                ChainContract(
                    strike=Decimal(str(r["strike"])),
                    option_type=r["option_type"],
                    expiry=_as_date(r["expiry"]),
                    open_interest=int(r["open_interest"]),
                    implied_volatility=float(r["implied_volatility"]),
                ),
            )
            spot_by_date[d] = Decimal(str(r["spot"]))
        if not by_date:
            return None
        dates = sorted(by_date)
        frozen = {
            d: (spot_by_date[d], tuple(by_date[d])) for d in dates
        }
        return _TickerIndex(dates=dates, by_date=frozen)


def _as_date(value: object) -> date:
    """Coerce a parquet date/datetime cell to a ``date``."""
    if isinstance(value, date):
        # datetime is a subclass of date; normalise to a pure date.
        return date(value.year, value.month, value.day)
    msg = f"expected a date-like snapshot cell, got {value!r}"
    raise TypeError(msg)
