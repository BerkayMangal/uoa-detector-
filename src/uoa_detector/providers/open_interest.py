"""``OpenInterestProvider`` Protocol — feeds Modules 27 and 28.

  - Module 27 (Opening vs Closing Interest Estimator) needs intraday
    OI snapshots to estimate whether incoming flow is opening (new
    positioning) or closing (unwinding existing).
  - Module 28 (Next-Day OI Confirmation) needs the post-market OI snapshot
    the day after each event to validate Module 27's estimate. The
    ``next_day`` method exists for this — it's queried by the M28
    background task scheduled to run after market close.

Phase 3 sources for both: IBKR (prior-close OI is reliable; intraday is
estimated by IBKR with caveats), CBOE end-of-day OI feed (authoritative
for next-day validation), Polygon snapshot endpoint.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class OpenInterestSnapshot(BaseModel):
    """OI snapshot for one option at one point in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    strike: Decimal
    expiry: date
    option_type: Literal["call", "put"]
    as_of: datetime  # tz-aware UTC
    open_interest: int


@runtime_checkable
class OpenInterestProvider(Protocol):
    """Source of OI snapshots per option, with a next-day method for M28."""

    async def at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        when: datetime,
    ) -> OpenInterestSnapshot | None:
        """Return the OI snapshot for the option at ``when`` (tz-aware UTC)."""
        ...

    async def next_day(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        trade_date: date,
    ) -> OpenInterestSnapshot | None:
        """Return the post-close OI snapshot for the trading day AFTER
        ``trade_date``.

        Used by Module 28's next-day validation: a flow event observed on
        ``trade_date`` is confirmed as opening interest if next_day OI
        increased proportionally; closing if OI decreased; ambiguous otherwise.

        Returns ``None`` if ``trade_date + 1`` hasn't completed yet (market
        still open) — caller schedules a retry after market close.
        """
        ...


class NoOpOpenInterestProvider:
    """Always returns ``None``. Module 27 leaves opening/closing estimation
    blank; Module 28's validation task no-ops gracefully.
    """

    async def at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        when: datetime,
    ) -> OpenInterestSnapshot | None:
        del ticker, strike, expiry, option_type, when
        return None

    async def next_day(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        trade_date: date,
    ) -> OpenInterestSnapshot | None:
        del ticker, strike, expiry, option_type, trade_date
        return None
