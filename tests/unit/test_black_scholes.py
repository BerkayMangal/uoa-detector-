"""Phase 3.6.2 — Black-Scholes primitives tests (GEX support)."""

from __future__ import annotations

import math

from uoa_detector.sources.thetadata_derived.black_scholes import (
    gamma,
    implied_vol,
    norm_cdf,
    norm_pdf,
    price,
)


def test_norm_pdf_cdf_reference_points() -> None:
    assert math.isclose(norm_pdf(0.0), 1.0 / math.sqrt(2 * math.pi))
    assert math.isclose(norm_cdf(0.0), 0.5)
    assert norm_cdf(-3.0) < 0.01
    assert norm_cdf(3.0) > 0.99


def test_gamma_atm_positive_degenerate_zero() -> None:
    assert gamma(100.0, 100.0, 0.1, 0.5) > 0.0
    assert gamma(100.0, 100.0, 0.0, 0.5) == 0.0
    assert gamma(100.0, 100.0, 0.1, 0.0) == 0.0


def test_price_call_put_and_intrinsic() -> None:
    c = price(100.0, 100.0, 0.25, 0.4, call=True)
    p = price(100.0, 100.0, 0.25, 0.4, call=False)
    assert c > 0.0 and p > 0.0
    assert math.isclose(c, p, rel_tol=1e-9)  # ATM, r=0 → call == put
    # Expired → intrinsic.
    assert price(110.0, 100.0, 0.0, 0.4, call=True) == 10.0
    assert price(90.0, 100.0, 0.0, 0.4, call=True) == 0.0


def test_implied_vol_round_trip() -> None:
    for sigma in (0.15, 0.4, 0.9, 1.5):
        obs = price(155.0, 150.0, 0.08, sigma, call=True)
        recovered = implied_vol(obs, 155.0, 150.0, 0.08, call=True)
        assert recovered is not None
        assert math.isclose(recovered, sigma, rel_tol=1e-3)


def test_implied_vol_out_of_band_returns_none() -> None:
    # A price above the no-arbitrage ceiling for IV ≤ 5 → None.
    assert implied_vol(1e9, 155.0, 150.0, 0.08, call=True) is None
    # Non-positive / degenerate inputs → None.
    assert implied_vol(0.0, 155.0, 150.0, 0.08, call=True) is None
    assert implied_vol(5.0, 155.0, 150.0, 0.0, call=True) is None
