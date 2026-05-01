"""``DarkPoolPrintProvider`` Protocol — feeds Module 26 (Dark Pool Confirmation).

Module 26 confirms options-flow signals against equity dark-pool prints:

  - DP prints below quoted bid → bullish absorption (someone soaking
    up sell pressure off-exchange).
  - DP prints above quoted ask → bearish distribution (someone unloading
    above market off-exchange).
  - DP prints at mid with subsequent same-direction drift → confirmation.

Phase 3 sources: SqueezeMetrics, BlackBoxStocks, NYSE TRF FINRA tape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class DarkPoolPrint(BaseModel):
    """One off-exchange print on the equity tape (TRF)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    when: datetime
    price: Decimal
    size: int  # share count
    side_estimate: Literal["above_ask", "at_or_below_bid", "midpoint", "unknown"]


@runtime_checkable
class DarkPoolPrintProvider(Protocol):
    """Source of recent equity dark-pool prints per ticker."""

    async def recent_prints(
        self,
        ticker: str,
        before: datetime,
        window: timedelta,
    ) -> Sequence[DarkPoolPrint]:
        """Return DP prints on ``ticker`` within ``window`` before ``before``."""
        ...


class NoOpDarkPoolPrintProvider:
    """Always returns empty. Module 26 falls back to no DP confirmation."""

    async def recent_prints(
        self,
        ticker: str,
        before: datetime,
        window: timedelta,
    ) -> Sequence[DarkPoolPrint]:
        del ticker, before, window
        return ()
