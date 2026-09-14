"""Phase 3.5.6 — falsification scoring tests.

Locks the mechanical verdict: each pinned reject scenario, the
sample-size gate, the partial-insufficiency N/A rule, and the
markdown rendering. The verdict must be deterministic — same input,
same output — so these tests double as the regression contract for
the four scenarios pinned in Phase 3.2.4.
"""

from __future__ import annotations

from pathlib import Path

from uoa_detector.backtest.cell_runner import CANONICAL_CELLS, CellRunResult
from uoa_detector.backtest.falsification import (
    apply_falsification,
    render_verdict_report,
    write_verdict_report,
)
from uoa_detector.backtest.metrics import BacktestMetrics

_SPEC_BY_NAME = {c.name: c for c in CANONICAL_CELLS}


def _metrics(
    *, total_trades: int, sharpe: float | None, wf: float | None = 0.8,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=total_trades,
        open_trades=0,
        sharpe=sharpe,
        expectancy=0.0,
        walk_forward_consistency=wf,
        max_drawdown=0.0,
        hit_rate=0.5,
        avg_winner_r=None,
        avg_loser_r=None,
        sharpe_bonus_flag=False,
        results=(),
        overall_pass=False,
    )


def _cell(
    name: str,
    *,
    total_trades: int = 50,
    sharpe: float | None = 1.0,
    wf: float | None = 0.8,
) -> CellRunResult:
    return CellRunResult(
        cell=_SPEC_BY_NAME[name],
        run_id=f"run-{name}",
        metrics=_metrics(total_trades=total_trades, sharpe=sharpe, wf=wf),
        windows=(),
    )


def _insufficient(name: str) -> CellRunResult:
    """A cell below the sample-size gate (sharpe is None, as metrics.py emits)."""
    return _cell(name, total_trades=10, sharpe=None, wf=None)


# ---------------------------------------------------------------------------
# Sample-size gate
# ---------------------------------------------------------------------------


def test_all_insufficient_is_insufficient_not_rejected() -> None:
    results = [
        _insufficient("tier1_single"),
        _insufficient("tier1_fusion"),
        _insufficient("tier2_single"),
        _insufficient("tier2_fusion"),
    ]
    v = apply_falsification(results)
    assert v.verdict == "insufficient"
    assert v.triggered_scenarios == ()
    assert set(v.insufficient_cells) == {
        "tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion",
    }
    assert v.computable_cells == ()
    assert v.best_cell_name is None


def test_just_below_gate_is_insufficient() -> None:
    # 29 closed trades — one short of the pinned gate of 30.
    results = [
        _cell("tier1_single", total_trades=29, sharpe=2.0),
        _cell("tier1_fusion", total_trades=29, sharpe=2.0),
        _cell("tier2_single", total_trades=29, sharpe=2.0),
        _cell("tier2_fusion", total_trades=29, sharpe=2.0),
    ]
    v = apply_falsification(results)
    assert v.verdict == "insufficient"


def test_exactly_at_gate_is_computable() -> None:
    # 30 closed trades — exactly the gate; cells are computable.
    results = [
        _cell("tier1_single", total_trades=30, sharpe=1.0),
        _cell("tier1_fusion", total_trades=30, sharpe=1.0),
        _cell("tier2_single", total_trades=30, sharpe=1.0),
        _cell("tier2_fusion", total_trades=30, sharpe=1.0),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_proven"
    assert len(v.computable_cells) == 4


# ---------------------------------------------------------------------------
# Reject scenarios
# ---------------------------------------------------------------------------


def test_scenario1_all_four_negative_rejected() -> None:
    results = [
        _cell("tier1_single", sharpe=-0.4),
        _cell("tier1_fusion", sharpe=-0.4),
        _cell("tier2_single", sharpe=-0.4),
        _cell("tier2_fusion", sharpe=-0.4),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert 1 in v.triggered_scenarios


def test_scenario1_requires_all_four_computable() -> None:
    # Three computable + all negative, one INSUFFICIENT. Scenario 1 is
    # defined as "all FOUR cells Sharpe < 0" — with only three computable
    # it must NOT fire. (Scenario 4 still fires on the negative best cell.)
    results = [
        _cell("tier1_single", sharpe=-0.4),
        _cell("tier1_fusion", sharpe=-0.4),
        _cell("tier2_single", sharpe=-0.4),
        _insufficient("tier2_fusion"),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert 1 not in v.triggered_scenarios
    assert 4 in v.triggered_scenarios


def test_scenario2_tier1_positive_tier2_negative_rejected() -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _cell("tier2_single", sharpe=-1.0),
        _cell("tier2_fusion", sharpe=-1.0),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert 2 in v.triggered_scenarios
    # The single-vs-fusion comparison nets to zero on each side — not 3.
    assert 3 not in v.triggered_scenarios


def test_scenario3_single_positive_fusion_negative_rejected() -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier2_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=-1.0),
        _cell("tier2_fusion", sharpe=-1.0),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert 3 in v.triggered_scenarios
    assert 2 not in v.triggered_scenarios


def test_scenario4_low_sharpe_rejected() -> None:
    # All positive but the best cell falls short of the 0.5 Sharpe bar.
    results = [
        _cell("tier1_single", sharpe=0.3),
        _cell("tier1_fusion", sharpe=0.3),
        _cell("tier2_single", sharpe=0.3),
        _cell("tier2_fusion", sharpe=0.3),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert v.triggered_scenarios == (4,)


def test_scenario4_low_walk_forward_rejected() -> None:
    # Best cell has a strong Sharpe but fails the 0.75 walk-forward bar.
    results = [
        _cell("tier1_single", sharpe=1.5, wf=0.5),
        _cell("tier1_fusion", sharpe=1.0, wf=0.9),
        _cell("tier2_single", sharpe=1.0, wf=0.9),
        _cell("tier2_fusion", sharpe=1.0, wf=0.9),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_rejected"
    assert 4 in v.triggered_scenarios
    assert v.best_cell_name == "tier1_single"


# ---------------------------------------------------------------------------
# Edge proven
# ---------------------------------------------------------------------------


def test_edge_proven_when_no_scenario_fires() -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _cell("tier2_single", sharpe=1.0),
        _cell("tier2_fusion", sharpe=1.0),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_proven"
    assert v.triggered_scenarios == ()
    assert v.scenarios_na == ()
    assert v.best_cell_name in _SPEC_BY_NAME


def test_best_cell_is_highest_sharpe() -> None:
    results = [
        _cell("tier1_single", sharpe=0.8),
        _cell("tier1_fusion", sharpe=2.4),
        _cell("tier2_single", sharpe=1.1),
        _cell("tier2_fusion", sharpe=0.9),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_proven"
    assert v.best_cell_name == "tier1_fusion"


# ---------------------------------------------------------------------------
# Partial insufficiency — cross-comparison N/A rule
# ---------------------------------------------------------------------------


def test_partial_insufficiency_marks_cross_comparison_na() -> None:
    # Tier-2 entirely below the gate: scenario 2 (t1 vs t2) needs both
    # sides computable, so it is N/A. Verdict relies on what remains.
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _insufficient("tier2_single"),
        _insufficient("tier2_fusion"),
    ]
    v = apply_falsification(results)
    assert v.verdict == "edge_proven"
    assert 2 in v.scenarios_na
    assert set(v.insufficient_cells) == {"tier2_single", "tier2_fusion"}
    assert set(v.computable_cells) == {"tier1_single", "tier1_fusion"}


def test_missing_cell_treated_as_insufficient() -> None:
    # Only three cells supplied — the fourth is missing, not zero-trade.
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _cell("tier2_single", sharpe=1.0),
    ]
    v = apply_falsification(results)
    assert "tier2_fusion" in v.insufficient_cells


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_verdict_is_deterministic() -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=-1.0),
        _cell("tier2_single", sharpe=1.0),
        _cell("tier2_fusion", sharpe=-1.0),
    ]
    assert apply_falsification(results) == apply_falsification(results)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_render_rejected_report_lists_triggered_scenarios() -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _cell("tier2_single", sharpe=-1.0),
        _cell("tier2_fusion", sharpe=-1.0),
    ]
    v = apply_falsification(results)
    md = render_verdict_report(v, results, period_label="2025-05 → 2026-04")
    assert "EDGE REJECTED" in md
    assert "Scenario 2" in md
    assert "2025-05 → 2026-04" in md
    for name in _SPEC_BY_NAME:
        assert name in md


def test_render_insufficient_report() -> None:
    results = [
        _insufficient("tier1_single"),
        _insufficient("tier1_fusion"),
        _insufficient("tier2_single"),
        _insufficient("tier2_fusion"),
    ]
    v = apply_falsification(results)
    md = render_verdict_report(v, results)
    assert "INSUFFICIENT" in md


def test_write_verdict_report_creates_file(tmp_path: Path) -> None:
    results = [
        _cell("tier1_single", sharpe=1.0),
        _cell("tier1_fusion", sharpe=1.0),
        _cell("tier2_single", sharpe=1.0),
        _cell("tier2_fusion", sharpe=1.0),
    ]
    v = apply_falsification(results)
    out = tmp_path / "nested" / "phase-3.5-results.md"
    write_verdict_report(v, results, out, period_label="test-period")
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "EDGE PROVEN" in content
    assert "test-period" in content
