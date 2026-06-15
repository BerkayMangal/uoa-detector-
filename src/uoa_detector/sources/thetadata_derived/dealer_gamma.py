"""Self-derived dealer gamma / GEX provider (Phase 3.6.1).

Computes the ``DealerExposureAggregate`` (net dealer gamma + zero-gamma
flip strike) that Module 21 consumes, from the option chain's open
interest and implied volatility — no Unusual Whales. The M21 stage's
scoring is unchanged; this only supplies the aggregate.

Methodology is frozen in ``docs/phase-3.6-acceptance.md``:

  - per-contract Black-Scholes gamma (calls and puts identical), r = 0;
  - SqueezeMetrics sign convention — dealers long calls, short puts:
    ``net = Σ sign · gamma · OI · 100 · S²·0.01`` (USD per 1% spot move),
    ``sign = +1`` call, ``−1`` put; negative ⇒ dealers net short;
  - flip strike = the hypothetical spot where total GEX crosses zero,
    found by scanning GEX(S) over the strike range and interpolating the
    lowest crossing (None if the curve is monotone);
  - point-in-time: the chain comes from the snapshot source as of the
    most-recent day ≤ the event instant (the leakage guard);
  - the GEX math is cached per (ticker, snapshot_date) — every event on a
    ticker-day reuses one computation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioning,
)
from uoa_detector.sources.thetadata_derived.black_scholes import (
    gamma as _bs_gamma,
)

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal

    from uoa_detector.sources.thetadata_derived.chain import (
        ChainAsOf,
        ChainContract,
        ChainSnapshotSource,
    )

_FLIP_SCAN_STEPS = 300


def _contract_dollar_gamma(
    spot: float, contract: ChainContract, as_of: date,
) -> float:
    """Signed dealer dollar-gamma for one contract, USD per 1% spot move."""
    t_years = (contract.expiry - as_of).days / 365.0
    gamma = _bs_gamma(spot, float(contract.strike), t_years, contract.implied_volatility)
    sign = 1.0 if contract.option_type == "call" else -1.0
    return sign * gamma * contract.open_interest * 100.0 * spot * spot * 0.01


def _net_gamma(
    spot: float, contracts: tuple[ChainContract, ...], as_of: date,
) -> float:
    return sum((_contract_dollar_gamma(spot, c, as_of) for c in contracts), 0.0)


def _flip_strike(
    spot: float, contracts: tuple[ChainContract, ...], as_of: date,
) -> float | None:
    """Lowest spot level at which total GEX crosses zero, or None."""
    strikes = [float(c.strike) for c in contracts]
    if not strikes:
        return None
    lo = min(min(strikes), spot) * 0.7
    hi = max(max(strikes), spot) * 1.3
    if lo <= 0.0:
        lo = hi * 0.01
    if hi <= lo:
        return None
    prev_s = lo
    prev_g = _net_gamma(lo, contracts, as_of)
    for i in range(1, _FLIP_SCAN_STEPS + 1):
        s = lo + (hi - lo) * i / _FLIP_SCAN_STEPS
        g = _net_gamma(s, contracts, as_of)
        if prev_g == 0.0:
            return prev_s
        if (prev_g < 0.0) != (g < 0.0):
            # Linear interpolation of the crossing between prev_s and s.
            frac = prev_g / (prev_g - g)
            return prev_s + frac * (s - prev_s)
        prev_s, prev_g = s, g
    return None


class ThetaDataDealerPositioningProvider:
    """``DealerPositioningProvider`` backed by the ThetaData chain.

    Satisfies the Protocol M21 consumes; computes net gamma + flip from
    the injected chain-snapshot source. Pure compute — no IO in the async
    methods (the source does its own reads).
    """

    def __init__(self, source: ChainSnapshotSource) -> None:
        self._source = source
        # (ticker, snapshot_date) -> (net_gamma_dollars, flip_strike) | None
        self._cache: dict[
            tuple[str, date], tuple[Decimal, Decimal | None] | None
        ] = {}

    async def aggregate_for_ticker(
        self, ticker: str, at: datetime,
    ) -> DealerExposureAggregate | None:
        chain = self._source.as_of(ticker, at)
        if chain is None or not chain.contracts:
            return None
        key = (ticker, chain.snapshot_date)
        if key not in self._cache:
            self._cache[key] = _compute_aggregate(chain)
        computed = self._cache[key]
        if computed is None:
            return None
        net_gamma, flip = computed
        return DealerExposureAggregate(
            ticker=ticker,
            as_of=at,
            net_gamma_dollars=net_gamma,
            flip_strike=flip,
        )

    async def net_gamma_at(
        self, ticker: str, strike: Decimal, at: datetime,
    ) -> DealerPositioning | None:
        chain = self._source.as_of(ticker, at)
        if chain is None:
            return None
        spot = float(chain.spot)
        if spot <= 0.0:
            return None
        at_strike = tuple(c for c in chain.contracts if c.strike == strike)
        if not at_strike:
            return None
        from decimal import Decimal as _Decimal

        net = _net_gamma(spot, at_strike, chain.snapshot_date)
        return DealerPositioning(
            ticker=ticker,
            strike=strike,
            as_of=at,
            net_gamma_dollars=_Decimal(str(net)),
            flow_direction="neutral",
        )


def _compute_aggregate(
    chain: ChainAsOf,
) -> tuple[Decimal, Decimal | None] | None:
    """The cached GEX math for one (ticker, snapshot_date)."""
    from decimal import Decimal as _Decimal

    spot = float(chain.spot)
    if spot <= 0.0:
        return None
    net = _net_gamma(spot, chain.contracts, chain.snapshot_date)
    flip = _flip_strike(spot, chain.contracts, chain.snapshot_date)
    return (
        _Decimal(str(net)),
        _Decimal(str(flip)) if flip is not None else None,
    )
