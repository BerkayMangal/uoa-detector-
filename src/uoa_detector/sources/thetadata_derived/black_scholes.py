"""Black-Scholes primitives for the self-derived GEX path (Phase 3.6).

One home for the BS math the GEX pipeline needs: gamma (dealer-positioning
provider) and price + implied-vol inversion (snapshot build derives IV
from the eod mid because ThetaData exposes no IV/greeks endpoint at this
tier). Risk-free rate r = 0 throughout — its effect on short-DTE gamma is
third-order (the download is DTE ≤ 60).
"""

from __future__ import annotations

import math

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_IV_LOW = 1e-4
_IV_HIGH = 5.0
_IV_ITERS = 60


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def gamma(spot: float, strike: float, t_years: float, iv: float) -> float:
    """BS gamma (calls == puts), r = 0. Degenerate inputs → 0."""
    if spot <= 0.0 or strike <= 0.0 or t_years <= 0.0 or iv <= 0.0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * sqrt_t)
    return norm_pdf(d1) / (spot * iv * sqrt_t)


def price(spot: float, strike: float, t_years: float, iv: float, *, call: bool) -> float:
    """BS option price, r = 0. Degenerate inputs → intrinsic value."""
    if t_years <= 0.0 or iv <= 0.0 or spot <= 0.0 or strike <= 0.0:
        intrinsic = (spot - strike) if call else (strike - spot)
        return max(0.0, intrinsic)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if call:
        return spot * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_vol(
    observed_price: float,
    spot: float,
    strike: float,
    t_years: float,
    *,
    call: bool,
) -> float | None:
    """Bisection-invert IV from an observed price, or None if not solvable.

    Returns None when inputs are degenerate or the price is outside the
    no-arbitrage band the BS model can produce for IV in [_IV_LOW, _IV_HIGH]
    — those contracts are dropped from the GEX sum rather than forced.
    """
    if observed_price <= 0.0 or spot <= 0.0 or strike <= 0.0 or t_years <= 0.0:
        return None
    lo, hi = _IV_LOW, _IV_HIGH
    price_lo = price(spot, strike, t_years, lo, call=call)
    price_hi = price(spot, strike, t_years, hi, call=call)
    if not (price_lo <= observed_price <= price_hi):
        return None
    for _ in range(_IV_ITERS):
        mid = 0.5 * (lo + hi)
        if price(spot, strike, t_years, mid, call=call) > observed_price:
            hi = mid
        else:
            lo = mid
    iv = 0.5 * (lo + hi)
    if iv <= _IV_LOW * 1.5 or iv >= _IV_HIGH * 0.99:
        return None
    return iv
