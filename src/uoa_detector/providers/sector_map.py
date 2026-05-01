"""Module 25 providers — ``SectorMapProvider`` + ``PeerFlowProvider``.

Module 25 confirms a signal by checking that the ticker's sector peers
show same-direction flow within a 30–90 minute window. Two distinct
data sources:

  1. **Sector mapping**: ticker → sector (or sub-sector) — slow-changing
     reference data, fits ``SectorMapProvider``.
  2. **Peer flow**: recent flow events on a peer ticker — derived from
     the same flow stream the detector consumes, queryable by ticker.

Phase 3 sources for sector map: GICS, ICB, custom user override. Phase 3
sources for peer flow: the detector's own backtest store (the simplest
path), or a separate aggregated feed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class PeerFlowEvent(BaseModel):
    """A flow event on a peer ticker, used for sector-confirmation logic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    direction: Literal["bullish", "bearish", "neutral"]
    when: datetime
    label: str | None = None  # original signal label if known (CONVEXITY_CLUSTER, etc.)


@runtime_checkable
class SectorMapProvider(Protocol):
    """Source of ticker → sector mappings."""

    async def sector_of(self, ticker: str) -> str | None:
        """Return ``ticker``'s sector identifier, or ``None`` if unknown."""
        ...

    async def peers_of(self, ticker: str) -> Sequence[str]:
        """Return tickers in the same sector as ``ticker`` (excluding ``ticker``)."""
        ...


@runtime_checkable
class PeerFlowProvider(Protocol):
    """Source of recent flow events on peer tickers."""

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        """Return flow events on ``tickers`` within ``window`` before ``before``."""
        ...


class InMemorySectorMapProvider:
    """Test/CLI implementation backed by a ``dict[ticker, sector]``."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = dict(mapping)
        # Reverse index for peers_of lookups.
        self._reverse: dict[str, list[str]] = {}
        for t, sector in self._map.items():
            self._reverse.setdefault(sector, []).append(t)

    async def sector_of(self, ticker: str) -> str | None:
        return self._map.get(ticker)

    async def peers_of(self, ticker: str) -> Sequence[str]:
        sector = self._map.get(ticker)
        if sector is None:
            return ()
        return tuple(t for t in self._reverse.get(sector, ()) if t != ticker)


class NoOpSectorMapProvider:
    """Always returns ``None`` / empty. Module 25 falls back to neutral confirmation."""

    async def sector_of(self, ticker: str) -> str | None:
        del ticker
        return None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        del ticker
        return ()


class NoOpPeerFlowProvider:
    """Always returns empty. Module 25 falls back to neutral confirmation."""

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        del tickers, before, window
        return ()
