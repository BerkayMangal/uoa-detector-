"""``DealerPositioningProvider`` Protocol — feeds Module 21 (Dealer Gamma Overlay).

Module 21 computes ``gamma_score`` and the GAMMA_ACCELERATION_RISK flag from
estimated dealer net gamma exposure at a given strike. Sources for this in
Phase 3:

  - ``GammaSwap`` / ``SpotGamma`` style aggregators that publish daily-
    estimated dealer GEX curves per ticker.
  - User's own derivation from OI + classification (calls long, puts short
    near ATM, etc.) using IBKR or Polygon snapshot data.

The Protocol abstracts both. Stages call ``net_gamma_at(ticker, strike, at)``
and get a positioning summary; the score-derivation logic stays in the
stage so calibration tunables (DTE thresholds, proximity bands) work
through ``profile`` rather than the provider.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class DealerPositioning(BaseModel):
    """Snapshot of estimated dealer positioning at a (ticker, strike, time).

    ``net_gamma_dollars`` is signed: negative = dealers short gamma (selling
    into rallies), positive = dealers long gamma (buying into rallies).
    Magnitude is in USD per 1% spot move.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    strike: Decimal
    as_of: datetime
    net_gamma_dollars: Decimal
    flow_direction: Literal["accumulating", "distributing", "neutral"]


@runtime_checkable
class DealerPositioningProvider(Protocol):
    """Source of dealer-positioning estimates per (ticker, strike, time)."""

    async def net_gamma_at(
        self,
        ticker: str,
        strike: Decimal,
        at: datetime,
    ) -> DealerPositioning | None:
        """Return positioning at ``at`` (tz-aware UTC), or ``None`` if unknown."""
        ...


class NoOpDealerPositioningProvider:
    """Always returns ``None`` — Module 21 falls back to a neutral score."""

    async def net_gamma_at(
        self,
        ticker: str,
        strike: Decimal,
        at: datetime,
    ) -> DealerPositioning | None:
        del ticker, strike, at
        return None
