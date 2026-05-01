"""``IVHistoryProvider`` Protocol — feeds Module 24 (IV Exhaustion Filter).

Module 24 penalises signals that arrive after IV has already exploded —
classic late-entry trap. The provider supplies historical IV per option:

  - IV time series for the contract (bid/ask IV at recent timestamps)
  - IV rank (current vs N-day range, 0..100)
  - IV percentile (current vs N-day distribution, 0..100)

Phase 3 sources: IBKR historical IV, ORATS, IVolatility, user-derived
from saved snapshot streams. The Protocol returns a typed series; the
exhaustion-detection logic (e.g., "IV expanded > 30% intraday — penalise")
stays in the stage with profile tunables.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class IVRankSnapshot(BaseModel):
    """IV rank/percentile snapshot for one option at one timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    strike: Decimal
    expiry: date
    option_type: Literal["call", "put"]
    as_of: datetime
    implied_volatility: float
    iv_rank_252d: float | None = None  # current IV vs 252-day range, 0..100
    iv_percentile_252d: float | None = None  # current IV vs 252-day distribution
    iv_change_intraday_pct: float | None = None  # intraday change since open


@runtime_checkable
class IVHistoryProvider(Protocol):
    """Source of historical IV and rank/percentile per option."""

    async def iv_rank_at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> IVRankSnapshot | None:
        """Return the IV-rank snapshot at ``at`` (tz-aware UTC), or ``None``."""
        ...


class NoOpIVHistoryProvider:
    """Always returns ``None``. Module 24 leaves IV exhaustion fields untouched."""

    async def iv_rank_at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> IVRankSnapshot | None:
        del ticker, strike, expiry, option_type, at
        return None
