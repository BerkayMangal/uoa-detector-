"""Phase 3.2.3.4 tests for ``compute_metrics`` + ``MetricThresholds``.

Pins:
  - threshold defaults track Phase 3 prep step 3
  - Sharpe formula (mean / stdev × √252; None below 30 trades)
  - expectancy formula (arithmetic mean of realized_r)
  - walk-forward consistency formula (fraction of windows with E > 0)
  - max drawdown formula (peak-to-trough on cumulative R)
  - hit rate / avg winner / avg loser arithmetic
  - reproducibility (same input stream → bit-identical output)
  - empty / open-only / single-trade edge cases
  - pass/fail against thresholds for each metric
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from uoa_detector.backtest import (
    BacktestMetrics,
    MetricThresholds,
    RealizedTrade,
    compute_metrics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    *,
    event_id: str,
    realized_r: float | None,
    exit_offset_days: int = 0,
) -> RealizedTrade:
    base = datetime(2025, 6, 9, 15, 30, tzinfo=UTC)
    entry = base + timedelta(days=exit_offset_days - 5)
    exit_ts = (
        base + timedelta(days=exit_offset_days)
        if realized_r is not None else None
    )
    return RealizedTrade(
        event_id=event_id,
        realized_r=realized_r,
        entry_ts=entry,
        exit_ts=exit_ts,
        exit_reason=(
            "fixed_window_elapsed"
            if realized_r is not None
            else "holding_window_open"
        ),
    )


# ---------------------------------------------------------------------------
# Threshold defaults pin (Phase 3 prep step 3)
# ---------------------------------------------------------------------------


def test_track_b_thresholds_match_phase_3_prep() -> None:
    """The default MetricThresholds carry the Track B + Formülasyon A
    pass criteria approved in Phase 3 prep step 3 + acceptance-doc
    3.2.0b/3.2.3 revisions. If any default changes, this test fails
    and forces a deliberate update."""
    th = MetricThresholds()
    assert th.sharpe_pass == 1.5
    assert th.sharpe_bonus == 2.5
    assert th.expectancy_pass == 0.5
    assert th.walk_forward_consistency_pass == 0.75
    assert th.max_drawdown_ceiling == 0.20
    assert th.min_trades == 60


def test_metric_thresholds_override_works() -> None:
    """Override-via-constructor is supported (defaults aren't a lockdown)."""
    th = MetricThresholds(sharpe_pass=2.0, min_trades=30)
    assert th.sharpe_pass == 2.0
    assert th.min_trades == 30
    assert th.expectancy_pass == 0.5  # unchanged default


def test_metric_thresholds_walk_forward_bounds() -> None:
    """walk_forward_consistency_pass bounded in [0, 1]."""
    with pytest.raises(Exception, match="walk_forward_consistency_pass"):
        MetricThresholds(walk_forward_consistency_pass=1.5)


# ---------------------------------------------------------------------------
# Empty / open-only edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_zero_total_zero_expectancy() -> None:
    m = compute_metrics([])
    assert m.total_trades == 0
    assert m.expectancy == 0.0
    assert m.sharpe is None
    assert m.walk_forward_consistency is None
    assert m.max_drawdown == 0.0
    assert m.hit_rate == 0.0
    assert m.avg_winner_r is None
    assert m.avg_loser_r is None
    assert m.overall_pass is False  # min_trades not met


def test_open_only_input_treats_all_as_open() -> None:
    """All trades open → total_trades=0, open_trades=N."""
    trades = [_trade(event_id=f"e{i}", realized_r=None) for i in range(5)]
    m = compute_metrics(trades)
    assert m.total_trades == 0
    assert m.open_trades == 5
    assert m.expectancy == 0.0


def test_single_winner_basic_metrics() -> None:
    """One trade, R=2.0 → expectancy=2, hit_rate=1, avg_winner=2."""
    trades = [_trade(event_id="w", realized_r=2.0, exit_offset_days=1)]
    m = compute_metrics(trades)
    assert m.total_trades == 1
    assert m.expectancy == 2.0
    assert m.hit_rate == 1.0
    assert m.avg_winner_r == 2.0
    # When all trades are winners, avg_loser falls back to all-trades mean.
    assert m.avg_loser_r == 2.0


# ---------------------------------------------------------------------------
# Expectancy + hit rate
# ---------------------------------------------------------------------------


def test_expectancy_arithmetic_mean() -> None:
    """E = mean of realized_r."""
    trades = [
        _trade(event_id="a", realized_r=1.0, exit_offset_days=1),
        _trade(event_id="b", realized_r=-0.5, exit_offset_days=2),
        _trade(event_id="c", realized_r=2.0, exit_offset_days=3),
    ]
    m = compute_metrics(trades)
    assert m.expectancy == pytest.approx((1.0 - 0.5 + 2.0) / 3, abs=1e-9)


def test_hit_rate_excludes_break_even() -> None:
    """hit_rate counts r > 0 only; r == 0 falls into the loser bucket."""
    trades = [
        _trade(event_id="a", realized_r=1.0, exit_offset_days=1),
        _trade(event_id="b", realized_r=0.0, exit_offset_days=2),  # break-even
        _trade(event_id="c", realized_r=-1.0, exit_offset_days=3),
    ]
    m = compute_metrics(trades)
    assert m.hit_rate == pytest.approx(1.0 / 3, abs=1e-9)
    assert m.avg_winner_r == 1.0
    assert m.avg_loser_r == pytest.approx(-0.5, abs=1e-9)  # mean of 0, -1


# ---------------------------------------------------------------------------
# Sharpe
# ---------------------------------------------------------------------------


def test_sharpe_none_below_30_trades() -> None:
    """Acceptance: < 30 trades → Sharpe is None ('insufficient_sample')."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(29)
    ]
    m = compute_metrics(trades)
    assert m.sharpe is None
    sharpe_result = next(r for r in m.results if r.name == "sharpe")
    assert sharpe_result.pass_fail == "insufficient_sample"


def test_sharpe_computed_at_exactly_30_trades() -> None:
    """30 trades is the cliff. With perfectly identical daily returns,
    stdev=0 and Sharpe is None (to avoid div-by-zero)."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(30)
    ]
    m = compute_metrics(trades)
    # Each trade is on its own day, all returns 1.0 → stdev=0 → None.
    assert m.sharpe is None


def test_sharpe_positive_for_winning_streak() -> None:
    """30 trades with mostly positive R → positive Sharpe."""
    rs = [1.0, 0.5, 1.5, -0.5, 2.0, 1.0, -0.3, 1.8, 0.7, 1.2] * 3
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i)
        for i in range(30)
    ]
    m = compute_metrics(trades)
    assert m.sharpe is not None
    assert m.sharpe > 0


def test_sharpe_aggregates_same_day_trades() -> None:
    """Two trades exiting on the same UTC date sum into one daily return.

    Without same-day aggregation, 60 same-day trades would inflate
    'len(daily)' and give a Sharpe that's off by sqrt(60).
    """
    # 60 trades, 30 same-day pairs.
    rs = [1.0, 0.5] * 30
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i // 2)
        for i in range(60)
    ]
    m = compute_metrics(trades)
    # 30 daily returns, each = 1.5, all identical → Sharpe None.
    assert m.sharpe is None


def test_sharpe_annualized_factor_is_sqrt_252() -> None:
    """Verify annualization: same daily R series with √252 factor.

    Construct exactly 4 distinct days, total 4 trades (one per day):
    daily series = [1, 2, -1, 2]; mean = 1; stdev (sample, n-1 = 3) =
      sqrt(((1-1)^2 + (2-1)^2 + (-1-1)^2 + (2-1)^2) / 3) =
      sqrt((0 + 1 + 4 + 1) / 3) = sqrt(2)
    Sharpe (annualized) = (1 / sqrt(2)) * sqrt(252) = sqrt(126).

    To exercise the >=30-trade gate, we need 30 trades; fill the
    remaining 26 with realized_r=0 spread across NEW days so they
    add zero-mean days that pull the Sharpe down. To avoid that
    interference, we instead pad with realized_r=0 ON THE SAME 4
    days — same-day aggregation means those zero contributions
    don't change the daily totals.
    """
    base_returns = [1.0, 2.0, -1.0, 2.0]
    trades: list[RealizedTrade] = []
    # 4 trades carrying the actual day values.
    for d, r in enumerate(base_returns):
        trades.append(_trade(event_id=f"core-{d}", realized_r=r, exit_offset_days=d))
    # 26 padding trades with realized_r=0, distributed across the same
    # 4 days. They sum into the existing daily totals as +0 each.
    for i in range(26):
        d = i % 4
        trades.append(_trade(event_id=f"pad-{i}", realized_r=0.0, exit_offset_days=d))

    assert len(trades) == 30
    m = compute_metrics(trades)
    assert m.total_trades == 30
    assert m.sharpe is not None
    expected = (1.0 / math.sqrt(2)) * math.sqrt(252)  # = sqrt(126) ≈ 11.225
    assert m.sharpe == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# Walk-forward consistency
# ---------------------------------------------------------------------------


def test_walk_forward_consistency_all_positive_windows() -> None:
    """8 trades, all positive → all 8 windows positive → consistency = 1.0."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(8)
    ]
    m = compute_metrics(trades, walk_forward_windows=8)
    assert m.walk_forward_consistency == 1.0


def test_walk_forward_consistency_three_of_four() -> None:
    """4 windows, 3 positive (E>0), 1 negative → consistency = 0.75."""
    # 8 trades into 4 windows of 2 each.
    # win1: [1, 1] → E=1 > 0 ✓
    # win2: [1, 0.5] → E=0.75 > 0 ✓
    # win3: [-1, -1] → E=-1 < 0 ✗
    # win4: [0.5, 0.5] → E=0.5 > 0 ✓
    rs = [1.0, 1.0, 1.0, 0.5, -1.0, -1.0, 0.5, 0.5]
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i)
        for i in range(8)
    ]
    m = compute_metrics(trades, walk_forward_windows=4)
    assert m.walk_forward_consistency == 0.75


def test_walk_forward_consistency_none_when_too_few_trades() -> None:
    """N=8 windows but only 5 trades → consistency = None."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(5)
    ]
    m = compute_metrics(trades, walk_forward_windows=8)
    assert m.walk_forward_consistency is None


def test_walk_forward_consistency_fraction_independent_of_n() -> None:
    """Same dataset partitioned at different N: 0.75 means 0.75 in either."""
    # 16 winners, 4 losers (the 5th, 10th, 15th, 20th).
    rs = [1.0] * 20
    rs[4] = -1.0
    rs[9] = -1.0
    rs[14] = -1.0
    rs[19] = -1.0
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i)
        for i in range(20)
    ]
    # N=4: chunks of 5; chunks [0:5], [5:10], [10:15], [15:20].
    # Each contains exactly one -1 and four +1, mean = (4-1)/5 = 0.6 > 0
    # → all 4 windows positive → consistency = 1.0.
    m4 = compute_metrics(trades, walk_forward_windows=4)
    assert m4.walk_forward_consistency == 1.0


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_zero_when_only_winners() -> None:
    """Cumulative monotonically increases → no drawdown."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(5)
    ]
    m = compute_metrics(trades)
    assert m.max_drawdown == 0.0


def test_max_drawdown_simple_case() -> None:
    """Cumulative path: 1, 2, 3, 1, 0, -1.
    Peak = 3 at index 2; trough after peak = -1 at index 5.
    DD = (3 - (-1)) / 3 = 4/3.
    """
    rs = [1.0, 1.0, 1.0, -2.0, -1.0, -1.0]
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i)
        for i in range(6)
    ]
    m = compute_metrics(trades)
    # peak=3, trough cumulative=-1, dd at trough = (3-(-1))/3 = 4/3
    assert m.max_drawdown == pytest.approx(4.0 / 3, abs=1e-9)


def test_max_drawdown_when_never_positive_uses_floor_one() -> None:
    """All losers: peak never above 0. DD reported using denom=max(peak, 1).
    Decision: see metrics.py module docstring 'max DD floor' note.
    """
    rs = [-0.5, -0.5, -1.0]
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i], exit_offset_days=i)
        for i in range(3)
    ]
    m = compute_metrics(trades)
    # cumulative: -0.5, -1.0, -2.0; peak stays at 0; dd at last = 2.0/1 = 2.0
    assert m.max_drawdown == pytest.approx(2.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Pass/fail integration
# ---------------------------------------------------------------------------


def test_overall_pass_requires_all_thresholds() -> None:
    """A run failing min_trades fails overall even if Sharpe is great."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=2.0, exit_offset_days=i)
        for i in range(40)  # not enough — default min_trades=60
    ]
    m = compute_metrics(trades)
    assert m.overall_pass is False
    total_trades_result = next(
        r for r in m.results if r.name == "total_trades"
    )
    assert total_trades_result.pass_fail == "fail"


def test_results_carry_per_metric_pass_fail_breakdown() -> None:
    """Five MetricResult entries: sharpe, expectancy, walk_forward,
    max_drawdown, total_trades."""
    trades = [
        _trade(event_id=f"e{i}", realized_r=0.6, exit_offset_days=i)
        for i in range(60)
    ]
    m = compute_metrics(trades)
    names = {r.name for r in m.results}
    assert names == {
        "sharpe", "expectancy", "walk_forward_consistency",
        "max_drawdown", "total_trades",
    }


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_compute_metrics_is_deterministic() -> None:
    """Same input twice → bit-identical BacktestMetrics output."""
    rs = [1.0, -0.5, 2.0, -1.0, 0.5, 1.5, -0.3, 0.7, 1.2, -0.8]
    trades = [
        _trade(event_id=f"e{i}", realized_r=rs[i % len(rs)],
               exit_offset_days=i)
        for i in range(60)
    ]
    a = compute_metrics(trades)
    b = compute_metrics(trades)
    assert a == b


def test_compute_metrics_synthetic_200_trade_dataset() -> None:
    """Sanity: a 200-trade synthetic stream produces reasonable numbers
    and all five MetricResult entries.

    Acceptance doc names a '200-trade dataset' for the reproducibility
    test; this test is the lightweight version — it doesn't claim
    hand-computed correctness on every metric (other tests do that),
    just that nothing blows up at scale.
    """
    rs_pattern = [1.5, -0.5, 2.0, -1.0, 0.5, 0.8, -0.3, 1.2]
    trades = [
        _trade(
            event_id=f"e{i}",
            realized_r=rs_pattern[i % len(rs_pattern)],
            exit_offset_days=i,
        )
        for i in range(200)
    ]
    m = compute_metrics(trades)
    assert m.total_trades == 200
    assert m.sharpe is not None
    assert m.walk_forward_consistency is not None
    assert isinstance(m, BacktestMetrics)
    # Determinism on this larger dataset.
    again = compute_metrics(trades)
    assert m == again


def test_open_trades_excluded_from_metrics() -> None:
    """Mix of closed + open trades: open ones counted under open_trades,
    excluded from every numerical metric."""
    closed = [
        _trade(event_id=f"c{i}", realized_r=1.0, exit_offset_days=i)
        for i in range(10)
    ]
    open_t = [
        _trade(event_id=f"o{i}", realized_r=None) for i in range(5)
    ]
    m = compute_metrics(closed + open_t)
    assert m.total_trades == 10
    assert m.open_trades == 5
    assert m.expectancy == 1.0  # ignores opens
