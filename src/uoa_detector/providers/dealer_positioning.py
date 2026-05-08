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


class DealerExposureAggregate(BaseModel):
    """Ticker-aggregated dealer positioning at a point in time.

    Phase 3.4.1: introduced for M21 (Dealer gamma exposure score)
    which scores at the ticker level (net gamma + zero-gamma flip
    strike), not the per-strike level that ``DealerPositioning``
    represents.

    ``net_gamma_dollars`` is the SUM across all listed strikes for
    the ticker — a magnitude that reflects dealer-wide exposure.
    Same sign convention as ``DealerPositioning``: negative ⇒
    dealers net short, positive ⇒ dealers net long.

    ``flip_strike`` is the strike at which the cumulative gamma
    profile crosses zero (the 'zero-gamma' strike). May be None
    if the curve is monotone (always net-long or always net-short
    across all strikes published) — M21 treats None as no proximity
    bonus.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    as_of: datetime
    net_gamma_dollars: Decimal
    flip_strike: Decimal | None


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

    async def aggregate_for_ticker(
        self,
        ticker: str,
        at: datetime,
    ) -> DealerExposureAggregate | None:
        """Return ticker-aggregate dealer exposure at ``at``, or None.

        Phase 3.4.1: feeds M21 (Dealer gamma exposure score). Sums
        net gamma across all strikes published for the ticker and
        identifies the flip strike (where cumulative gamma crosses
        zero). Returns None when no data is published for the
        ticker (illiquid name, new listing, etc.); M21 treats None
        as score = None and does not raise.
        """
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

    async def aggregate_for_ticker(
        self,
        ticker: str,
        at: datetime,
    ) -> DealerExposureAggregate | None:
        del ticker, at
        return None
