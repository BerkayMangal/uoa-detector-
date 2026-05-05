"""Phase 3.2.4.3 tests for ``render_4cell_comparison_report``.

Pins:
  - all 4 cells appear in the rendered table
  - 'tier1_single' is the default baseline; deltas computed against it
  - Tier-2 fusion shows the biggest expectancy delta in a winning fixture
  - 'What would falsify Formülasyon A' section present with hypothesis text
  - n/a rendered for None Sharpe / walk-forward
  - bonus Sharpe gets the star marker
  - per-metric pass/fail breakdown table renders all 4 cells × 5 metrics
  - empty results raises
  - report is deterministic (same input → identical bytes)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uoa_detector.backtest import (
    BacktestStore,
    RealizedTrade,
    fixture_trade_producer,
    render_4cell_comparison_report,
    run_4cell_backtest,
)
from uoa_detector.calibration import load_default_profile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trades(prefix: str, count: int, r: float) -> list[RealizedTrade]:
    base = datetime(2024, 6, 1, tzinfo=UTC)
    return [
        RealizedTrade(
            event_id=f"{prefix}-{i}",
            realized_r=r,
            entry_ts=base + timedelta(days=i),
            exit_ts=base + timedelta(days=i + 5),
            exit_reason="fixed_window_elapsed",
        )
        for i in range(count)
    ]


def _run_4cell(fixtures: dict[str, list[RealizedTrade]]) -> tuple:  # type: ignore[type-arg]
    profile = load_default_profile()
    store = BacktestStore()
    return run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=fixture_trade_producer(fixtures),
    )


# ---------------------------------------------------------------------------
# Header + structure
# ---------------------------------------------------------------------------


def test_report_header_present() -> None:
    results = _run_4cell({})
    md = render_4cell_comparison_report(results)
    assert "# 4-cell comparison report" in md


def test_report_names_baseline_explicitly() -> None:
    results = _run_4cell({})
    md = render_4cell_comparison_report(results)
    assert "Baseline cell:" in md
    assert "tier1_single" in md


def test_report_includes_all_four_cells() -> None:
    """Every cell name appears in the rendered table."""
    fixtures = {
        c: _trades(c, 60, 0.5)
        for c in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion")
    }
    results = _run_4cell(fixtures)
    md = render_4cell_comparison_report(results)
    for cell in (
        "tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion",
    ):
        assert f"`{cell}`" in md


def test_report_includes_falsification_section() -> None:
    """Hypothesis-restatement section is the heart of the 4-cell report."""
    md = render_4cell_comparison_report(_run_4cell({}))
    assert "What would falsify Formülasyon A" in md
    # Key clauses from the falsification text.
    assert "fusion is necessary" in md
    assert "Tier-2 is" in md
    assert "Combinatorial hypothesis is decisively rejected" in md


def test_report_includes_per_metric_pass_fail_breakdown() -> None:
    """The per-metric breakdown table appears after the 4×5 table."""
    fixtures = {
        c: _trades(c, 60, 0.5)
        for c in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion")
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    assert "Per-metric pass/fail breakdown" in md
    assert "PASS" in md or "FAIL" in md or "INSUFFICIENT" in md


# ---------------------------------------------------------------------------
# Delta column behaviour
# ---------------------------------------------------------------------------


def test_baseline_cell_no_delta_in_its_own_row() -> None:
    """The baseline cell's row shows raw values, no delta column."""
    fixtures = {
        c: _trades(c, 60, 0.5)
        for c in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion")
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    # Find the baseline row and check no delta marker on its own row.
    lines = md.splitlines()
    baseline_lines = [ln for ln in lines if "`tier1_single`" in ln]
    assert baseline_lines, "baseline row not found in report"
    # The baseline row in the main 4×5 table.
    main_baseline = [ln for ln in baseline_lines if "PASS" in ln or "FAIL" in ln]
    assert main_baseline
    # Δ marker should NOT appear in the baseline's own main-table row.
    assert "Δ" not in main_baseline[0]


def test_other_cells_show_delta_vs_baseline() -> None:
    """Non-baseline cells show '(Δ ...)' next to their values."""
    fixtures = {
        "tier1_single": _trades("t1s", 60, 0.3),
        "tier1_fusion": _trades("t1f", 60, 0.6),
        "tier2_single": _trades("t2s", 60, 0.4),
        "tier2_fusion": _trades("t2f", 60, 1.2),
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    # tier2_fusion has +0.9 expectancy delta vs baseline tier1_single (0.3).
    # The delta string is "Δ +0.900" with the up-arrow.
    assert "Δ +0.900" in md or "Δ +0.900 ↑" in md


def test_winning_cell_shows_largest_expectancy_delta() -> None:
    """When tier2_fusion is engineered to win, its delta is the largest
    positive delta in the table."""
    fixtures = {
        "tier1_single": _trades("t1s", 60, 0.2),
        "tier1_fusion": _trades("t1f", 60, 0.4),
        "tier2_single": _trades("t2s", 60, 0.5),
        "tier2_fusion": _trades("t2f", 60, 1.5),  # the winner
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    # Largest expectancy delta = 1.3 (1.5 - 0.2).
    assert "Δ +1.300" in md


# ---------------------------------------------------------------------------
# n/a rendering
# ---------------------------------------------------------------------------


def test_sharpe_renders_as_na_when_too_few_trades() -> None:
    """Cells with <30 trades show 'n/a' for Sharpe."""
    fixtures = {
        "tier1_single": _trades("t1s", 5, 1.0),
        "tier1_fusion": _trades("t1f", 5, 1.0),
        "tier2_single": _trades("t2s", 5, 1.0),
        "tier2_fusion": _trades("t2f", 5, 1.0),
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    # Sharpe column shows n/a; specifically every cell has n/a.
    assert md.count("n/a") >= 4  # at least 4 cells × 1 metric (sharpe) each


def test_walk_forward_renders_as_na_when_too_few_trades() -> None:
    """Cells with <N trades show 'n/a' for walk-forward."""
    fixtures = {
        "tier1_single": _trades("t1s", 4, 1.0),
        "tier1_fusion": _trades("t1f", 4, 1.0),
        "tier2_single": _trades("t2s", 4, 1.0),
        "tier2_fusion": _trades("t2f", 4, 1.0),
    }
    md = render_4cell_comparison_report(_run_4cell(fixtures))
    # walk-forward also n/a for all of these (4 < N=8).
    assert "n/a" in md


# ---------------------------------------------------------------------------
# Determinism + edge cases
# ---------------------------------------------------------------------------


def test_report_is_deterministic() -> None:
    """Same input → identical rendered output."""
    fixtures = {
        c: _trades(c, 30, 0.5)
        for c in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion")
    }
    a = render_4cell_comparison_report(_run_4cell(fixtures))
    b = render_4cell_comparison_report(_run_4cell(fixtures))
    assert a == b


def test_empty_results_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        render_4cell_comparison_report(())


def test_report_uses_fallback_baseline_when_named_baseline_missing() -> None:
    """If baseline_cell_name doesn't match any cell, fall back to first."""
    fixtures = {
        "tier2_fusion": _trades("t2f", 30, 1.0),
    }
    # Run only one cell.
    from uoa_detector.backtest import CellSpec
    profile = load_default_profile()
    store = BacktestStore()
    one_cell = (CellSpec(universe="tier2", fusion="fusion"),)
    results = run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=fixture_trade_producer(fixtures),
        cells=one_cell,
    )
    md = render_4cell_comparison_report(results, baseline_cell_name="tier1_single")
    # Falls back to tier2_fusion as baseline (the only cell present).
    assert "tier2_fusion" in md
