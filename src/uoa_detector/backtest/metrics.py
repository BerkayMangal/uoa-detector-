"""Metric calculator — converts trade outcomes into Track B success metrics.

Phase 3.2.3.4: pins the math. The five Track B + Formülasyon A
success metrics approved in Phase 3 prep step 3:

  * **Sharpe (annualized)**: mean / stdev of daily realized-R series,
    multiplied by √252. Returns ``None`` if total trades < 30.
  * **Expectancy E**: arithmetic mean of per-trade R.
  * **Walk-forward consistency**: trades sorted by exit timestamp,
    partitioned into N equal-trade-count windows; consistency =
    fraction of windows whose E > 0. Real number in [0, 1].
  * **Max drawdown (%)**: peak-to-trough on cumulative-R curve, as
    percentage of running peak. (When peak is 0 or negative, max DD
    is reported as 0 — degenerate cumulative path.)
  * **Total trades**: count of decisions with non-zero max_r AND
    realized_r non-None.

Plus three secondary diagnostics:

  * **Hit rate**: fraction of total trades with realized_r > 0.
  * **Avg winner R**: mean realized_r over winning trades only;
    None if zero winners.
  * **Avg loser R**: mean realized_r over non-winning trades; if all
    trades were winners, falls back to mean of all trades.

The default ``MetricThresholds`` carry the Track B + Formülasyon A
pass criteria. Defaults pinned by
``test_track_b_thresholds_match_phase_3_prep``; override via
constructor is supported, defaults cannot drift without breaking the
test.

decision (open-trade exclusion):
  Open trades (realized_r=None) are excluded from every metric. They
  contribute to nothing — not Sharpe, not E, not walk-forward
  consistency. They DO count towards a separate diagnostic
  ``open_trades`` field so backtest reports can flag runs where a
  large fraction of decisions never closed (a signal that the
  holding window is too long for the dataset, or the data window
  ended mid-trade).

decision (max DD floor):
  When the cumulative-R curve never goes positive (e.g., all losers),
  every "drawdown" is from the initial 0. We report max DD as
  ``-cumulative_R / 1.0`` rather than dividing by the running peak
  (which is 0 → undefined). This matches the Phase 3 prep intent of
  "max DD as percentage" while staying numerically defined.

decision (Sharpe min sample):
  30 trades is below typical statistical comfort, but the Phase 3
  prep approved success criteria explicitly require ≥30 trades to
  trust the Sharpe value. Below 30 we return None and the threshold
  comparison reports `None vs > 1.5` as 'insufficient_sample' rather
  than 'fail'. Pass/fail logic in MetricThresholds handles this.

decision (annualization factor 252):
  US trading days per year. Same convention as the Phase 3 prep
  doc; same convention as everything in pandas-quant land.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.backtest.pnl_provider import RealizedTrade

# ---------------------------------------------------------------------------
# Threshold + comparison shape
# ---------------------------------------------------------------------------


class MetricThresholds(BaseModel):
    """Track B + Formülasyon A pass criteria.

    Defaults are the Phase 3 prep step 3 numbers + acceptance-doc
    3.2.0b/3.2.3 revisions. Override the constructor for special-case
    backtests; the defaults are pinned by tests.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sharpe_pass: float = Field(
        default=1.5,
        description="Sharpe > this is a pass.",
    )
    sharpe_bonus: float = Field(
        default=2.5,
        description="Sharpe > this is a separately-flagged 'bonus' result.",
    )
    expectancy_pass: float = Field(
        default=0.5,
        description="Expectancy > this (in R units) is a pass.",
    )
    walk_forward_consistency_pass: float = Field(
        default=0.75, ge=0.0, le=1.0,
        description=(
            "Fraction of walk-forward windows with E > 0. N-independent."
        ),
    )
    max_drawdown_ceiling: float = Field(
        default=0.20, ge=0.0, le=1.0,
        description=(
            "Maximum drawdown < this fraction is a pass. 0.20 = 20%."
        ),
    )
    min_trades: int = Field(
        default=60, ge=1,
        description="Total trades must be >= this for the run to be meaningful.",
    )


_PassFail = Literal["pass", "fail", "insufficient_sample"]


class MetricResult(BaseModel):
    """Per-metric pass/fail breakdown plus the underlying number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float | None
    threshold: float | None
    pass_fail: _PassFail


class BacktestMetrics(BaseModel):
    """Full metric output for one backtest run.

    Carries the raw numbers plus the pass/fail breakdown against the
    supplied thresholds. The 4-cell runner (Phase 3.2.4) renders these
    into a comparison report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_trades: int
    open_trades: int
    sharpe: float | None
    expectancy: float
    walk_forward_consistency: float | None
    max_drawdown: float
    hit_rate: float
    avg_winner_r: float | None
    avg_loser_r: float | None
    sharpe_bonus_flag: bool
    results: tuple[MetricResult, ...]
    overall_pass: bool


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: list[RealizedTrade],
    *,
    thresholds: MetricThresholds | None = None,
    walk_forward_windows: int = 8,
) -> BacktestMetrics:
    """Compute the full metric set from an iterable of RealizedTrade.

    Open trades (realized_r=None) are counted under ``open_trades`` and
    excluded from every numerical metric.

    Parameters:
      ``thresholds`` — pass/fail thresholds. ``None`` uses the Track B
        defaults.
      ``walk_forward_windows`` — N for the walk-forward consistency
        partition. Acceptance default 8; profiles can override (3.2.4
        wires this from BacktestConfig).
    """
    th = thresholds or MetricThresholds()
    closed = [t for t in trades if t.realized_r is not None]
    open_trades = sum(1 for t in trades if t.realized_r is None)

    total_trades = len(closed)
    rs = [t.realized_r for t in closed if t.realized_r is not None]

    # Expectancy
    expectancy = float(sum(rs) / len(rs)) if rs else 0.0

    # Hit rate
    winners = [r for r in rs if r > 0]
    losers = [r for r in rs if r <= 0]
    hit_rate = float(len(winners) / total_trades) if total_trades else 0.0

    # Avg winner / loser R
    avg_winner = float(sum(winners) / len(winners)) if winners else None
    if losers:
        avg_loser: float | None = float(sum(losers) / len(losers))
    elif rs:
        avg_loser = float(sum(rs) / len(rs))
    else:
        avg_loser = None

    # Sharpe annualized — daily R series.
    sharpe = _sharpe_annualized(closed) if total_trades >= 30 else None
    sharpe_bonus_flag = sharpe is not None and sharpe > th.sharpe_bonus

    # Walk-forward consistency.
    wf_consistency = _walk_forward_consistency(closed, walk_forward_windows)

    # Max drawdown.
    max_dd = _max_drawdown(closed)

    # Pass/fail per threshold.
    results = (
        MetricResult(
            name="sharpe",
            value=sharpe,
            threshold=th.sharpe_pass,
            pass_fail=_compare_gt(sharpe, th.sharpe_pass),
        ),
        MetricResult(
            name="expectancy",
            value=expectancy,
            threshold=th.expectancy_pass,
            pass_fail=_compare_gt(
                expectancy if total_trades > 0 else None,
                th.expectancy_pass,
            ),
        ),
        MetricResult(
            name="walk_forward_consistency",
            value=wf_consistency,
            threshold=th.walk_forward_consistency_pass,
            pass_fail=_compare_ge(
                wf_consistency, th.walk_forward_consistency_pass,
            ),
        ),
        MetricResult(
            name="max_drawdown",
            value=max_dd,
            threshold=th.max_drawdown_ceiling,
            pass_fail=_compare_lt(max_dd, th.max_drawdown_ceiling),
        ),
        MetricResult(
            name="total_trades",
            value=float(total_trades),
            threshold=float(th.min_trades),
            pass_fail=_compare_ge(float(total_trades), float(th.min_trades)),
        ),
    )

    overall_pass = all(r.pass_fail == "pass" for r in results)

    return BacktestMetrics(
        total_trades=total_trades,
        open_trades=open_trades,
        sharpe=sharpe,
        expectancy=expectancy,
        walk_forward_consistency=wf_consistency,
        max_drawdown=max_dd,
        hit_rate=hit_rate,
        avg_winner_r=avg_winner,
        avg_loser_r=avg_loser,
        sharpe_bonus_flag=sharpe_bonus_flag,
        results=results,
        overall_pass=overall_pass,
    )


# ---------------------------------------------------------------------------
# Internal: each metric is its own helper for testability
# ---------------------------------------------------------------------------


def _sharpe_annualized(closed_trades: list[RealizedTrade]) -> float | None:
    """Annualized Sharpe ratio of the daily realized-R series.

    Daily aggregation: sum of realized_r per UTC date of exit_ts.
    Annualization factor: √252.

    Returns None if all daily returns are identical (stdev = 0).
    """
    by_day: defaultdict[date, float] = defaultdict(float)
    for t in closed_trades:
        if t.exit_ts is None or t.realized_r is None:
            continue
        by_day[t.exit_ts.date()] += t.realized_r

    daily = list(by_day.values())
    if len(daily) < 2:
        return None
    mean = sum(daily) / len(daily)
    var = sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)
    if var <= 0:
        return None
    stdev = math.sqrt(var)
    return (mean / stdev) * math.sqrt(252)


def _walk_forward_consistency(
    closed_trades: list[RealizedTrade], n: int,
) -> float | None:
    """Fraction of N equal-trade-count windows with E > 0.

    Trades sorted by exit_ts ascending; partition into N equal-size
    chunks (any remainder appended to the last chunk). Per-window
    expectancy E_i = mean of realized_r in that window. Consistency =
    count(E_i > 0) / N. Returns None if fewer than N trades available.
    """
    if n <= 0:
        msg = f"walk_forward_windows must be > 0, got {n}"
        raise ValueError(msg)
    if len(closed_trades) < n:
        return None
    sorted_trades = sorted(
        closed_trades, key=lambda t: t.exit_ts or datetime.min,
    )
    chunk_size = len(sorted_trades) // n
    positive_count = 0
    for i in range(n):
        if i == n - 1:
            window = sorted_trades[i * chunk_size :]
        else:
            window = sorted_trades[i * chunk_size : (i + 1) * chunk_size]
        rs = [t.realized_r for t in window if t.realized_r is not None]
        if not rs:
            continue
        if sum(rs) / len(rs) > 0:
            positive_count += 1
    return positive_count / n


def _max_drawdown(closed_trades: list[RealizedTrade]) -> float:
    """Peak-to-trough drawdown on cumulative-R, as fraction of running peak.

    Trades ordered by exit_ts ascending; cumulative R is running sum.
    DD at index i = (peak_so_far - cumulative[i]) / max(peak_so_far, 1).
    Reports the maximum DD across the path. Always >= 0.
    """
    if not closed_trades:
        return 0.0
    sorted_trades = sorted(
        closed_trades, key=lambda t: t.exit_ts or datetime.min,
    )
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        if t.realized_r is None:
            continue
        cumulative += t.realized_r
        if cumulative > peak:
            peak = cumulative
        # Use max(peak, 1) so the never-positive case still produces a
        # numerically meaningful percentage. When peak=0 and cumulative
        # is negative, dd = -cumulative / 1 = absolute drawdown.
        denom = peak if peak > 1.0 else 1.0
        dd = (peak - cumulative) / denom
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compare_gt(
    value: float | None, threshold: float,
) -> _PassFail:
    if value is None:
        return "insufficient_sample"
    return "pass" if value > threshold else "fail"


def _compare_ge(
    value: float | None, threshold: float,
) -> _PassFail:
    if value is None:
        return "insufficient_sample"
    return "pass" if value >= threshold else "fail"


def _compare_lt(
    value: float | None, threshold: float,
) -> _PassFail:
    if value is None:
        return "insufficient_sample"
    return "pass" if value < threshold else "fail"
