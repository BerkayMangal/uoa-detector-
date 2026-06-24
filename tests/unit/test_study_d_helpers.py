"""Unit tests for the Study D statistic helpers (pre-reg §4 + addendum).

The whole D verdict rides on the CAUSAL conditioning being genuinely
look-ahead-free, and on the DSR/Welch maths being correct. The study lives in
``scripts/`` (outside the package), so it is imported here via an explicit path
insert. These tests lock the instrument BEFORE the fresh-window run — no study
data is touched, only synthetic inputs.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import study_D_conditioning_replication as sd  # noqa: E402  (path set above)


def test_causal_iv_pct_is_look_ahead_free() -> None:
    # Single ticker, IVs in date order. The causal percentile at row i must use
    # ONLY rows 0..i (expanding), never the full-window rank.
    ivs = [0.10, 0.50, 0.20, 0.90, 0.30]
    panel = pd.DataFrame({
        "ticker": ["TSLA"] * 5,
        "date": pd.date_range("2024-05-01", periods=5, freq="D"),
        "iv": ivs,
    })
    got = sd._causal_iv_pct(panel).tolist()
    # Expanding fraction of {prior+current iv <= current iv}:
    expected = [1.0, 1.0, 2 / 3, 1.0, 3 / 5]
    assert got == pytest.approx(expected)

    # And it must DIFFER from the full-window (look-ahead) rank — that
    # difference is exactly the look-ahead the addendum strips out.
    full_window = panel["iv"].rank(pct=True).tolist()
    assert got != pytest.approx(full_window)
    assert got[1] == pytest.approx(1.0)        # causal: 0.5 is the max so far
    assert full_window[1] == pytest.approx(0.8)  # look-ahead: 0.5 is 4th of 5


def test_causal_iv_pct_is_per_ticker() -> None:
    # Two tickers interleaved by date — each ticker's expanding window must be
    # independent (no cross-ticker leakage).
    panel = pd.DataFrame({
        "ticker": ["A", "B", "A", "B"],
        "date": pd.to_datetime(["2024-05-01", "2024-05-01", "2024-05-02", "2024-05-02"]),
        "iv": [0.2, 0.9, 0.1, 0.8],
    })
    # Assign back so pandas index-aligns the (sorted) result to each row —
    # exactly how the study script consumes it.
    panel["causal"] = sd._causal_iv_pct(panel)

    def val(ticker: str, day: str) -> float:
        m = (panel["ticker"] == ticker) & (panel["date"] == pd.Timestamp(day))
        return float(panel.loc[m, "causal"].iloc[0])

    # A: [0.2, 0.1] -> first 1.0, second 0.1<=0.1 only -> 0.5
    assert val("A", "2024-05-01") == pytest.approx(1.0)
    assert val("A", "2024-05-02") == pytest.approx(0.5)
    # B: [0.9, 0.8] -> first 1.0, second 0.5
    assert val("B", "2024-05-02") == pytest.approx(0.5)


def test_vrp_formula() -> None:
    panel = pd.DataFrame({"s_in": [100.0], "iv": [0.40], "move": [5.0]})
    out = sd._vrp(panel)
    implied = 100.0 * 0.40 * math.sqrt(20 / 252)
    assert out["vrp"].iloc[0] == pytest.approx((implied - 5.0) / 100.0)
    # vrp > 0 means implied move exceeded realised (vol overpriced).
    assert out["vrp"].iloc[0] > 0


def test_welch_t_matches_hand_computation() -> None:
    a = np.array([1.0, 2.0, 3.0, 4.0])
    b = np.array([2.0, 4.0, 6.0, 8.0])
    # mean_a=2.5 var_a=5/3 ; mean_b=5 var_b=20/3 ; denom=sqrt(va/4+vb/4)
    denom = math.sqrt((5 / 3) / 4 + (20 / 3) / 4)
    assert sd._welch_t(a, b) == pytest.approx((2.5 - 5.0) / denom)


def test_welch_t_degenerate_returns_nan() -> None:
    assert math.isnan(sd._welch_t(np.array([1.0]), np.array([2.0, 3.0])))
    assert math.isnan(sd._welch_t(np.array([5.0, 5.0]), np.array([5.0, 5.0])))


def test_dsr_n_trials_1_is_finite_psr_not_singular() -> None:
    # The whole point of the local _dsr: n_trials=1 must NOT hit the log(0)
    # singularity of the inherited deflated_sharpe — it returns a finite PSR-vs-0.
    rng = np.random.default_rng(0)
    r = rng.normal(0.05, 1.0, size=200)
    dsr = sd._dsr(r, n_trials=1)
    assert math.isfinite(dsr)
    assert 0.0 < dsr < 1.0
    # Independent recompute via the same PSR-vs-0 definition.
    sr = r.mean() / r.std(ddof=1)
    sk = float(pd.Series(r).skew())
    ku = float(pd.Series(r).kurt()) + 3.0
    denom = math.sqrt((1 - sk * sr + (ku - 1) / 4.0 * sr * sr) / (r.size - 1))
    assert dsr == pytest.approx(sd.ev._ncdf(sr / denom))


def test_dsr_too_few_obs_is_nan() -> None:
    assert math.isnan(sd._dsr(np.array([0.1, 0.2, 0.3]), n_trials=1))


def test_dsr_rises_with_mean() -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(0.0, 1.0, size=300)
    low = sd._dsr(base, n_trials=1)
    high = sd._dsr(base + 0.3, n_trials=1)  # shift mean up, same shape
    assert high > low
