"""Tests for the live UW gamma-map computation (Phase 4.19)."""

from __future__ import annotations

from webapp.gamma_live import context_from_uw


def _rows() -> list[dict[str, object]]:
    # Calls dominate above spot (100), puts below -> net crosses zero near spot.
    rows: list[dict[str, object]] = []
    for strike in range(80, 121, 5):
        call = 50.0 if strike >= 100 else 2.0
        put = -2.0 if strike >= 100 else -50.0
        rows.append({"strike": str(strike), "call_gex": str(call), "put_gex": str(put)})
    # a far-OTM noise strike that must be ignored for flip/walls
    rows.append({"strike": "5", "call_gex": "999", "put_gex": "0"})
    return rows


def _iv() -> dict[str, object]:
    return {"close": "100", "volatility": "0.42", "iv_rank_1y": "85.0", "date": "2026-06-22"}


def test_context_computes_regime_walls_iv() -> None:
    ctx = context_from_uw(_rows(), _iv())
    assert ctx is not None
    assert ctx["spot"] == 100.0
    assert ctx["atm_iv"] == 0.42
    assert ctx["iv_pct"] == 0.85  # iv_rank_1y / 100
    # call wall is the max-call-gex strike within the near-spot band (not $5)
    assert ctx["call_wall"] is not None and 80 <= ctx["call_wall"] <= 120  # type: ignore[operator]
    assert ctx["put_wall"] is not None and 80 <= ctx["put_wall"] <= 120  # type: ignore[operator]


def test_flip_lands_near_spot_not_noise_strike() -> None:
    ctx = context_from_uw(_rows(), _iv())
    assert ctx is not None and ctx["flip"] is not None
    assert 80.0 <= ctx["flip"] <= 120.0  # type: ignore[operator]  # near spot, not $5


def test_no_spot_returns_none() -> None:
    assert context_from_uw(_rows(), {"volatility": "0.4"}) is None


def test_empty_rows_returns_none() -> None:
    assert context_from_uw([], _iv()) is None
