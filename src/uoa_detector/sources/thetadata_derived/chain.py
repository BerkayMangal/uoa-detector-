"""Point-in-time option-chain snapshot interface (Phase 3.6).

The GEX provider needs, for a (ticker, instant), the chain that was in
force at that instant — every contract's last-known open interest and
implied volatility as of the most recent trading day on or before the
instant, plus the spot. ``ChainSnapshotSource`` abstracts where that comes
from; the daily-snapshot reader built in 3.6.2 implements it, and tests
inject a synthetic source.

The as-of selection (most-recent snapshot date ≤ the query instant, never
a future day) is the leakage guard and lives behind this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class ChainContract:
    """One contract's point-in-time state for the GEX sum."""

    strike: Decimal
    option_type: Literal["call", "put"]
    expiry: date
    open_interest: int
    implied_volatility: float


@dataclass(frozen=True)
class ChainAsOf:
    """The chain in force at a query instant.

    ``snapshot_date`` is the trading day the rows are drawn from (the
    most-recent day ≤ the query instant). It is the cache key for the GEX
    computation — every event on the same ticker-day sees the same chain.
    """

    ticker: str
    snapshot_date: date
    spot: Decimal
    contracts: tuple[ChainContract, ...]


@runtime_checkable
class ChainSnapshotSource(Protocol):
    """Source of point-in-time chains, keyed by (ticker, instant)."""

    def as_of(self, ticker: str, at: datetime) -> ChainAsOf | None:
        """Return the chain as of the most-recent snapshot date ≤ ``at``.

        Returns ``None`` when no snapshot on or before ``at`` exists for
        the ticker (e.g. before the data window, or an unknown ticker). A
        snapshot dated strictly after ``at`` must never be returned — that
        is the look-ahead guard.
        """
        ...
