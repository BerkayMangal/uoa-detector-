"""Tests for the screener's plain-English display helpers."""

from __future__ import annotations

from webapp.explanations import conviction, headline


def test_conviction_tiers() -> None:
    assert conviction(None) == ("—", 0)
    assert conviction(0.45)[0] == "Strong"
    assert conviction(0.32)[0] == "Moderate"
    assert conviction(0.22)[0] == "Weak"
    assert conviction(0.05)[0] == "Noise"


def test_conviction_meter_pct_clamped() -> None:
    assert conviction(0.25)[1] == 50  # 0.25/0.5 -> 50%
    assert conviction(0.9)[1] == 100  # clamped


def test_headline_bullish_call_sweep() -> None:
    h = headline("call", "SWEEP_UOA", swept=True, dte=8)
    assert h.startswith("Bullish call")
    assert "aggressive sweep" in h
    assert "swept across venues" in h
    assert "8 days to expiry" in h


def test_headline_bearish_put_expires_today() -> None:
    h = headline("put", "STANDARD_UOA", swept=False, dte=0)
    assert h.startswith("Bearish put")
    assert "expires today" in h
    assert "swept" not in h
