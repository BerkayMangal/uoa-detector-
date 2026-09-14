"""Phase 3.5.7 — sanity-audit tooling tests.

Pins the four audit checks (look-ahead leakage, trade frequency, PnL
distribution, spread/slippage) so they are ready to run on the verdict
run the moment it produces trades.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from uoa_detector.backtest.pnl_provider import RealizedTrade
from uoa_detector.backtest.sanity_audit import (
    assess_trade_frequency,
    check_lookahead,
    pnl_distribution,
    spread_sanity,
)

_BASE = datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


def _closed(event_id: str, *, r: float, hold_minutes: int = 60) -> RealizedTrade:
    return RealizedTrade(
        event_id=event_id,
        realized_r=r,
        entry_ts=_BASE,
        exit_ts=_BASE + timedelta(minutes=hold_minutes),
        exit_reason="fixed_window_elapsed",
    )


def _open(event_id: str) -> RealizedTrade:
    return RealizedTrade(
        event_id=event_id,
        realized_r=None,
        entry_ts=_BASE,
        exit_ts=None,
        exit_reason="holding_window_open",
    )


# ---------------------------------------------------------------------------
# Look-ahead leakage
# ---------------------------------------------------------------------------


def test_lookahead_clean_closed_trades_ok() -> None:
    trades = [_closed("a", r=1.0), _closed("b", r=-0.5), _open("c")]
    rep = check_lookahead(trades)
    assert rep.ok
    assert rep.violations == ()
    assert rep.closed_trades == 2  # the open trade is not counted


def test_lookahead_flags_exit_not_after_entry() -> None:
    same_ts = RealizedTrade(
        event_id="same",
        realized_r=0.3,
        entry_ts=_BASE,
        exit_ts=_BASE,  # exit == entry → time-travel
        exit_reason="fixed_window_elapsed",
    )
    backwards = RealizedTrade(
        event_id="back",
        realized_r=0.3,
        entry_ts=_BASE,
        exit_ts=_BASE - timedelta(minutes=1),  # exit before entry
        exit_reason="fixed_window_elapsed",
    )
    rep = check_lookahead([_closed("good", r=1.0), same_ts, backwards])
    assert not rep.ok
    assert set(rep.violations) == {"same", "back"}


# ---------------------------------------------------------------------------
# Trade frequency
# ---------------------------------------------------------------------------


def test_frequency_too_few() -> None:
    rep = assess_trade_frequency(19)
    assert rep.too_few and not rep.too_many
    assert "too_few" in rep.verdict


def test_frequency_too_many() -> None:
    rep = assess_trade_frequency(201)
    assert rep.too_many and not rep.too_few
    assert "too_many" in rep.verdict


def test_frequency_in_band_ok() -> None:
    for n in (20, 60, 200):
        rep = assess_trade_frequency(n)
        assert not rep.too_few and not rep.too_many
        assert rep.verdict == "ok"
        assert rep.target == 60


# ---------------------------------------------------------------------------
# PnL distribution
# ---------------------------------------------------------------------------


def test_distribution_empty() -> None:
    rep = pnl_distribution([_open("x")])
    assert rep.n == 0
    assert rep.mean_r is None and rep.win_rate is None


def test_distribution_stats() -> None:
    trades = [_closed("a", r=2.0), _closed("b", r=-1.0), _closed("c", r=1.0)]
    rep = pnl_distribution(trades)
    assert rep.n == 3
    assert rep.mean_r == 2.0 / 3
    assert rep.median_r == 1.0
    assert rep.min_r == -1.0
    assert rep.max_r == 2.0
    assert rep.win_rate == 2 / 3


# ---------------------------------------------------------------------------
# Spread (slippage) sanity
# ---------------------------------------------------------------------------


def test_spread_sanity_empty() -> None:
    rep = spread_sanity([], slippage_assumption_pct=0.02)
    assert rep.n == 0
    assert rep.median_pct is None
    assert not rep.optimistic


def test_spread_sanity_stats_and_optimistic_flag() -> None:
    # Median spread (0.03) exceeds the 0.02 assumption → optimistic flag.
    spreads = [0.01, 0.02, 0.03, 0.04, 0.10]
    rep = spread_sanity(spreads, slippage_assumption_pct=0.02)
    assert rep.n == 5
    assert rep.median_pct == 0.03
    assert rep.frac_over_assumption == 3 / 5  # 0.03, 0.04, 0.10
    assert rep.optimistic


def test_spread_sanity_not_optimistic_when_median_below_assumption() -> None:
    rep = spread_sanity([0.005, 0.01, 0.012], slippage_assumption_pct=0.02)
    assert not rep.optimistic
