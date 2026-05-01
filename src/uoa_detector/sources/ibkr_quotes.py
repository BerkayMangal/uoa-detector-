"""IBKR ``QuoteSnapshotSource`` adapter — stub for Phase 3 implementation.

IBKR (via TWS or IB Gateway) is NOT a flow stream — it's a request/reply
quote lookup. Different interface from ``RawFlowSource``: the pipeline
asks ``snapshot(ticker, strike, expiry, option_type, at)`` and gets back
a ``QuoteSnapshot`` carrying bid/ask/IV/OI valid for ``at``.

Why IBKR specifically (rather than another quote vendor)?
  - The user already runs IBKR for execution; using the same broker for
    quotes avoids an extra data subscription.
  - IBKR's IV is computed from OPRA quotes using a consistent model
    (Black-Scholes with their own dividend-and-rate inputs), which makes
    cross-comparison with historical IV stable.
  - OI updates daily after close; IBKR is one of the few sources that
    exposes both prior-close OI and intraday-recomputed OI, useful for
    Module 27/28 next-day-confirmation logic in Phase 3.

Expected fields per ``snapshot`` call:
  - ``bid``, ``ask``, ``last``: NBBO + last trade price at ``at``.
  - ``implied_volatility``: IBKR's BS-derived IV at ``at``.
  - ``open_interest``: prior session-end OI (intraday updates not
    reliable — Phase 3's Module 27/28 will use the next-day snapshot).
  - ``as_of``: the actual timestamp the snapshot is valid for. May
    differ from ``at`` if the requested time is between ticks; IBKR
    returns the most recent tick at-or-before ``at``.

Phase 3 wiring needs (TODOs, not implemented in Phase 2):
  - ``ib_async`` (or ``ib_insync``) client setup against TWS/Gateway.
  - Caching layer: a single trading session's snapshots accumulate fast,
    and many fusion buckets will request the same (contract, ts) pair.
    Use an LRU keyed by (ticker, strike, expiry, option_type, ts_minute).
  - Reconnection on TWS restart — IBKR's API drops connections at
    market-close maintenance windows.
  - Subscription budget: IBKR allows ~100 concurrent market-data lines
    by default; the cache + careful (only-when-needed) request policy is
    essential. The pipeline asks for a snapshot only when scoring requires
    it (Module 24 IV exhaustion, Module 36 OI-zero penalty), not for every
    flow print.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.errors import DataSourceError

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal

    from uoa_detector.sources.base import QuoteSnapshot


class IBKRConfig(BaseModel):
    """Connection settings for the IBKR adapter.

    No secret token — IBKR auth is desktop-app-side (TWS or IB Gateway).
    The Python side just connects to a local TCP port the user must have
    running and logged in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(
        default="127.0.0.1",
        description="TWS/Gateway host (env: UOA_IBKR__HOST)",
    )
    port: int = Field(
        default=7497,
        description=(
            "TWS/Gateway port. 7497 = paper TWS, 7496 = live TWS, 4002 = "
            "paper Gateway, 4001 = live Gateway. Default is paper to avoid "
            "accidentally hitting a live account during testing."
        ),
    )
    client_id: int = Field(
        default=42,
        ge=0,
        le=999,
        description=(
            "TWS API client ID. Each connection needs a unique ID; pick "
            "one not used by other tools (TWS itself uses 0)."
        ),
    )


class IBKRQuoteSource:
    """``QuoteSnapshotSource`` adapter for IBKR via TWS/Gateway.

    Phase 2 STUB: ``snapshot()`` raises ``DataSourceError``. Phase 3 will
    implement the ``ib_async`` connection and the LRU snapshot cache.

    Note that this implements ``QuoteSnapshotSource`` — point-in-time
    request/reply — NOT ``RawFlowSource``. IBKR can stream quotes but
    that's not how the v5 detector uses it; we treat IBKR as the
    authoritative IV/OI lookup, queried sparingly when scoring needs it.
    """

    source_id = "ibkr"

    def __init__(self, config: IBKRConfig) -> None:
        self._config = config
        self._closed = False

    async def snapshot(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> QuoteSnapshot:
        """Return a ``QuoteSnapshot`` valid for ``at`` (tz-aware UTC).

        Phase 2: not implemented. Phase 3 will materialise this from the
        IBKR session's market-data subscriptions, with an LRU cache to
        keep subscription count under the per-account limit.
        """
        del ticker, strike, expiry, option_type, at  # unused in stub
        msg = (
            "IBKRQuoteSource is a Phase 2 stub — TWS/Gateway client not yet "
            "wired. Phase 3 implements snapshot() against the live IBKR API."
        )
        raise DataSourceError(msg)

    async def close(self) -> None:
        """Idempotent close. Phase 3 will disconnect from TWS/Gateway here."""
        self._closed = True
