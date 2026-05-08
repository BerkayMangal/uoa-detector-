"""``PriceActionProvider`` Protocol — feeds Module 23 (Price Confirmation).

Module 23 uses intraday price action to confirm or contradict the
direction of an options flow signal. The provider supplies the intraday
state at a given timestamp:

  - VWAP relative to spot
  - HH/HL vs LH/LL pattern over recent bars
  - Prior-day range break (above prior high, below prior low)
  - Volume expansion vs trailing average

Phase 3 sources: Polygon aggregates (1-minute bars), IBKR historical
bars, user's own intraday cache. The Protocol returns a typed snapshot;
score derivation stays in the stage so the spec's bands are profile-tunable.

Phase 3.4.3 extension: ``get_intraday_price_movement(ticker, at,
lookback_minutes)`` returns a ``PriceMovement`` summary used by
Module 23's score branches. Additive — existing ``snapshot_at``
preserved for any callers (currently only NoOp).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class PriceActionSnapshot(BaseModel):
    """Intraday price-action state for ``ticker`` at ``as_of``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    as_of: datetime
    spot: Decimal
    vwap: Decimal
    prior_day_high: Decimal | None = None
    prior_day_low: Decimal | None = None
    direction: Literal["up", "down", "neutral"]
    higher_highs_higher_lows: bool = False  # bullish trend signal
    volume_vs_trailing_avg: float = 1.0  # 1.0 = at average, 2.0 = double, etc.


class PriceMovement(BaseModel):
    """Spot price movement over a fixed lookback window.

    Phase 3.4.3: feeds Module 23 (Price confirmation score). The
    ``move_pct`` is signed: positive = spot rose over the lookback,
    negative = spot fell.

    ``lookback_minutes_actual`` may be less than the requested
    lookback if the stage clamped the window to the session open.
    Stage-side clamping (M23 owns the policy) — provider just
    reports what it actually fetched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    as_of: datetime
    spot_at: Decimal
    spot_lookback_ago: Decimal
    move_pct: float
    lookback_minutes_actual: int


@runtime_checkable
class PriceActionProvider(Protocol):
    """Source of intraday price-action state."""

    async def snapshot_at(
        self,
        ticker: str,
        at: datetime,
    ) -> PriceActionSnapshot | None:
        """Return the price-action snapshot at ``at`` (tz-aware UTC).

        Returns ``None`` if the ticker isn't covered or ``at`` is outside
        market hours and no carry-over is computable.
        """
        ...

    async def get_intraday_price_movement(
        self,
        ticker: str,
        at: datetime,
        lookback_minutes: int,
    ) -> PriceMovement | None:
        """Return spot movement over [at - lookback_minutes, at].

        Phase 3.4.3: feeds M23. Returns ``None`` when spot data is
        missing or the requested window is unavailable (illiquid
        name, after-hours timestamp with no overnight carry, etc.);
        M23 treats ``None`` as the neutral score branch.

        The actual lookback used (``lookback_minutes_actual``) may
        be smaller than requested if the caller clamped it to a
        session boundary.
        """
        ...


class NoOpPriceActionProvider:
    """Always returns ``None``. Module 23 falls back to neutral confirmation."""

    async def snapshot_at(
        self,
        ticker: str,
        at: datetime,
    ) -> PriceActionSnapshot | None:
        del ticker, at
        return None

    async def get_intraday_price_movement(
        self,
        ticker: str,
        at: datetime,
        lookback_minutes: int,
    ) -> PriceMovement | None:
        del ticker, at, lookback_minutes
        return None
