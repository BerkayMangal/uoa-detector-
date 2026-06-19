"""Tests for the dealer-gamma map computation (Phase 4.10)."""

from __future__ import annotations

import pandas as pd
from webapp.gamma import compute_gamma


def _chain(call_oi: int, put_oi: int) -> pd.DataFrame:
    rows = []
    for strike in range(80, 121, 5):
        rows.append({"strike": strike, "expiry": "2026-08-21", "option_type": "call",
                     "open_interest": call_oi, "implied_volatility": 0.4, "spot": 100.0})
        rows.append({"strike": strike, "expiry": "2026-08-21", "option_type": "put",
                     "open_interest": put_oi, "implied_volatility": 0.4, "spot": 100.0})
    return pd.DataFrame(rows)


def test_call_heavy_chain_is_long_gamma() -> None:
    m = compute_gamma(_chain(call_oi=1000, put_oi=100), as_of="2026-06-22")
    assert m is not None
    assert m["net_gex"] > 0  # calls dominate -> dealers long gamma (suppress)


def test_put_heavy_chain_is_short_gamma() -> None:
    m = compute_gamma(_chain(call_oi=100, put_oi=1000), as_of="2026-06-22")
    assert m is not None
    assert m["net_gex"] < 0  # puts dominate -> short gamma (amplify)


def test_flip_is_near_spot() -> None:
    # Realistic book: puts dominate below spot, calls above -> net GEX crosses
    # zero near the current spot (100), so the flip should land in that region.
    rows = []
    for strike in range(80, 121, 5):
        rows.append({"strike": strike, "expiry": "2026-08-21", "option_type": "call",
                     "open_interest": 1000 if strike >= 100 else 100,
                     "implied_volatility": 0.4, "spot": 100.0})
        rows.append({"strike": strike, "expiry": "2026-08-21", "option_type": "put",
                     "open_interest": 1000 if strike <= 100 else 100,
                     "implied_volatility": 0.4, "spot": 100.0})
    m = compute_gamma(pd.DataFrame(rows), as_of="2026-06-22")
    assert m is not None and m["flip"] is not None
    assert 85.0 <= m["flip"] <= 115.0


def test_walls_are_valid_strikes() -> None:
    m = compute_gamma(_chain(call_oi=500, put_oi=500), as_of="2026-06-22")
    assert m is not None
    assert m["call_wall"] in range(80, 121)
    assert m["put_wall"] in range(80, 121)


def test_empty_chain_returns_none() -> None:
    empty = pd.DataFrame({
        "strike": [100.0], "expiry": ["2020-01-01"], "option_type": ["call"],
        "open_interest": [0], "implied_volatility": [0.0], "spot": [100.0],
    })
    assert compute_gamma(empty, as_of="2026-06-22") is None
