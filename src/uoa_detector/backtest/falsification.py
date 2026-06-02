"""Phase 3.5.6 — falsification scoring.

Read the 4-cell results, apply the four pre-pinned reject scenarios
plus the sample-size gate, emit a verdict. The verdict is mechanical:
no judgement-call wiggle room. The same input → the same verdict,
every time.

The four scenarios (Phase 3.2.4 → 3.5.6, pinned):

  1. All four cells Sharpe < 0 → REJECTED.
  2. Tier-1 cells positive but Tier-2 cells negative → REJECTED.
  3. Single-source cells positive but fusion cells negative → REJECTED.
  4. Best cell Sharpe < 0.5 OR walk-forward consistency < 0.75 → REJECTED.

Sample-size gate (Phase 3.5.2.1):

  A cell is "computable" iff ``total_trades >= min_closed_trades``
  (default 30 — matches the ``Sharpe`` threshold in ``metrics.py``).
  - All four INSUFFICIENT → run verdict = INSUFFICIENT (not REJECTED).
  - Some INSUFFICIENT, some computable: scenarios 1 and 4 run on the
    computable subset; the cross-comparisons 2 and 3 require BOTH
    sides of the comparison to be computable, otherwise N/A.

Verdict written to a markdown file (``docs/phase-3.5-results.md`` by
convention). The file is intentionally short — the per-cell detail
already lives in the comparison report.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.backtest.cell_runner import CellRunResult


_logger = logging.getLogger(__name__)


Verdict = Literal["edge_proven", "edge_rejected", "insufficient"]


@dataclass(frozen=True)
class FalsificationVerdict:
    """The mechanical verdict produced by ``apply_falsification``."""

    verdict: Verdict
    """One of: edge_proven, edge_rejected, insufficient."""

    triggered_scenarios: tuple[int, ...]
    """Reject scenarios that fired (1-4). Empty if edge_proven or insufficient."""

    insufficient_cells: tuple[str, ...]
    """Cell names whose closed-trade count is below the sample-size gate."""

    computable_cells: tuple[str, ...]
    """Cell names that ARE computable. The intersection of the run with
    the sample-size gate."""

    best_cell_name: str | None
    """For ``edge_proven``: the highest-Sharpe computable cell. None
    otherwise."""

    scenarios_na: tuple[int, ...]
    """Scenarios that could not be evaluated because one side of the
    cross-comparison was INSUFFICIENT (scenarios 2 and 3 only)."""

    rationale: str
    """Short human-readable explanation of why this verdict was reached."""


# Pinned thresholds — copied from the Phase 3.5.6 acceptance doc.
_MIN_CLOSED_TRADES = 30
_MIN_BEST_SHARPE = 0.5
_MIN_BEST_WALK_FORWARD = 0.75

_TIER1_CELL_NAMES = ("tier1_single", "tier1_fusion")
_TIER2_CELL_NAMES = ("tier2_single", "tier2_fusion")
_SINGLE_CELL_NAMES = ("tier1_single", "tier2_single")
_FUSION_CELL_NAMES = ("tier1_fusion", "tier2_fusion")


def apply_falsification(
    results: Sequence[CellRunResult],
) -> FalsificationVerdict:
    """Apply the Phase 3.5.6 falsification framework to a 4-cell run.

    ``results`` must contain all four canonical cells
    (tier1_single, tier1_fusion, tier2_single, tier2_fusion) in any
    order. Cells outside the canonical four are ignored.

    Returns a verdict object — no markdown rendering, no IO.
    """
    by_name = {r.cell.name: r for r in results}

    # Sample-size gate
    insufficient: list[str] = []
    computable: list[str] = []
    for name in (*_TIER1_CELL_NAMES, *_TIER2_CELL_NAMES):
        r = by_name.get(name)
        if r is None or r.metrics.total_trades < _MIN_CLOSED_TRADES:
            insufficient.append(name)
        else:
            computable.append(name)

    if not computable:
        return FalsificationVerdict(
            verdict="insufficient",
            triggered_scenarios=(),
            insufficient_cells=tuple(insufficient),
            computable_cells=(),
            best_cell_name=None,
            scenarios_na=(),
            rationale=(
                "All four cells fell below the sample-size gate "
                f"({_MIN_CLOSED_TRADES} closed trades). Verdict: "
                "edge unverified, sample too small. Treat as a Phase "
                "3.5.3 follow-up (extend the download window)."
            ),
        )

    # Scenarios 1 + 4 evaluate over the computable subset.
    # Scenarios 2 + 3 need BOTH sides computable; otherwise N/A.
    triggered: list[int] = []
    scenarios_na: list[int] = []

    sharpe_by_cell: dict[str, float | None] = {
        name: by_name[name].metrics.sharpe
        for name in computable
    }

    # Scenario 1 — every COMPUTABLE cell has Sharpe < 0.
    # (Cells without a Sharpe — None — don't count as <0; conservative.)
    computable_sharpes: list[float] = [
        s for n in computable
        if (s := sharpe_by_cell[n]) is not None
    ]
    if (
        len(computable_sharpes) == len(computable)
        and len(computable) == 4
        and all(s < 0 for s in computable_sharpes)
    ):
        triggered.append(1)

    # Scenario 2 — tier-1 positive on average, tier-2 negative on average.
    s2 = _evaluate_cross_comparison(
        _TIER1_CELL_NAMES, _TIER2_CELL_NAMES,
        sharpe_by_cell, computable,
    )
    if s2 == "trigger":
        triggered.append(2)
    elif s2 == "na":
        scenarios_na.append(2)

    # Scenario 3 — single-source positive, fusion negative.
    s3 = _evaluate_cross_comparison(
        _SINGLE_CELL_NAMES, _FUSION_CELL_NAMES,
        sharpe_by_cell, computable,
    )
    if s3 == "trigger":
        triggered.append(3)
    elif s3 == "na":
        scenarios_na.append(3)

    # Scenario 4 — best cell falls short.
    best_name = _pick_best_cell(by_name, computable)
    if best_name is None:
        # No computable cell with a finite Sharpe: scenario 4 is N/A.
        scenarios_na.append(4)
    else:
        best = by_name[best_name].metrics
        best_sharpe = best.sharpe or float("-inf")
        wf = best.walk_forward_consistency
        if best_sharpe < _MIN_BEST_SHARPE or (
            wf is not None and wf < _MIN_BEST_WALK_FORWARD
        ):
            triggered.append(4)

    if triggered:
        return FalsificationVerdict(
            verdict="edge_rejected",
            triggered_scenarios=tuple(sorted(triggered)),
            insufficient_cells=tuple(insufficient),
            computable_cells=tuple(computable),
            best_cell_name=best_name,
            scenarios_na=tuple(sorted(scenarios_na)),
            rationale=_rejected_rationale(triggered, best_name, by_name),
        )

    # No scenario fired → edge_proven.
    return FalsificationVerdict(
        verdict="edge_proven",
        triggered_scenarios=(),
        insufficient_cells=tuple(insufficient),
        computable_cells=tuple(computable),
        best_cell_name=best_name,
        scenarios_na=tuple(sorted(scenarios_na)),
        rationale=(
            f"None of the four reject scenarios fired. Best cell: "
            f"{best_name}. Recommend Phase 3.5.7 sanity audit before "
            "proceeding to Phase 4."
        ),
    )


def _evaluate_cross_comparison(
    positive_side: tuple[str, ...],
    negative_side: tuple[str, ...],
    sharpe_by_cell: dict[str, float | None],
    computable: list[str],
) -> Literal["trigger", "ok", "na"]:
    """Return whether a (pos > 0 AND neg < 0) cross-comparison fires.

    Returns ``"na"`` when either side has no computable cell with a
    Sharpe. ``"trigger"`` when the positive side averages > 0 and the
    negative side averages < 0. ``"ok"`` otherwise.
    """
    pos: list[float] = [
        s for n in positive_side
        if n in computable and (s := sharpe_by_cell[n]) is not None
    ]
    neg: list[float] = [
        s for n in negative_side
        if n in computable and (s := sharpe_by_cell[n]) is not None
    ]
    if not pos or not neg:
        return "na"
    pos_mean = statistics.mean(pos)
    neg_mean = statistics.mean(neg)
    if pos_mean > 0 and neg_mean < 0:
        return "trigger"
    return "ok"


def _pick_best_cell(
    by_name: dict[str, CellRunResult], computable: list[str],
) -> str | None:
    """Highest-Sharpe computable cell. None when no computable cell has a finite Sharpe."""
    finite: list[tuple[str, float]] = [
        (name, s)
        for name in computable
        if (s := by_name[name].metrics.sharpe) is not None
    ]
    if not finite:
        return None
    finite.sort(key=lambda kv: kv[1], reverse=True)
    return finite[0][0]


def _rejected_rationale(
    triggered: list[int],
    best_name: str | None,
    by_name: dict[str, CellRunResult],
) -> str:
    """Short explanation of which scenarios fired."""
    pieces: list[str] = []
    if 1 in triggered:
        pieces.append("S1: all four cells Sharpe < 0")
    if 2 in triggered:
        pieces.append("S2: tier-1 positive on average, tier-2 negative")
    if 3 in triggered:
        pieces.append("S3: single-source positive, fusion negative")
    if 4 in triggered and best_name is not None:
        best = by_name[best_name].metrics
        s = best.sharpe
        wf = best.walk_forward_consistency
        pieces.append(
            f"S4: best cell ({best_name}) Sharpe="
            f"{(s if s is not None else float('nan')):.2f}, "
            f"walk-forward={(wf if wf is not None else float('nan')):.2f}",
        )
    return "REJECTED — " + "; ".join(pieces) + "."


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_verdict_report(
    verdict: FalsificationVerdict,
    results: Sequence[CellRunResult],
    *,
    period_label: str | None = None,
) -> str:
    """Render the Phase 3.5 results doc as markdown.

    Output is short — the per-cell number-detail lives in the
    comparison report. This doc captures the verdict + reasoning so
    Phase 3.5.6 has a single committable artefact.
    """
    by_name = {r.cell.name: r for r in results}
    lines: list[str] = []
    lines.append("# Phase 3.5 — falsification verdict")
    lines.append("")
    if period_label:
        lines.append(f"Backtest period: {period_label}")
        lines.append("")

    verdict_label = {
        "edge_proven": "**EDGE PROVEN**",
        "edge_rejected": "**EDGE REJECTED**",
        "insufficient": "**INSUFFICIENT — edge unverified**",
    }[verdict.verdict]
    lines.append(f"## Verdict: {verdict_label}")
    lines.append("")
    lines.append(verdict.rationale)
    lines.append("")

    # Per-cell metric summary
    lines.append("## Cells")
    lines.append("")
    lines.append("| cell | closed trades | Sharpe | walk-forward | state |")
    lines.append("|---|---|---|---|---|")
    for name in (*_TIER1_CELL_NAMES, *_TIER2_CELL_NAMES):
        r = by_name.get(name)
        if r is None:
            lines.append(f"| `{name}` | — | — | — | MISSING |")
            continue
        m = r.metrics
        state = (
            "INSUFFICIENT" if name in verdict.insufficient_cells
            else "computable"
        )
        sharpe = (
            f"{m.sharpe:+.3f}" if m.sharpe is not None else "n/a"
        )
        wf = (
            f"{m.walk_forward_consistency:+.2f}"
            if m.walk_forward_consistency is not None else "n/a"
        )
        marker = (
            " ⭐"
            if name == verdict.best_cell_name and verdict.verdict == "edge_proven"
            else ""
        )
        lines.append(
            f"| `{name}`{marker} | {m.total_trades} | "
            f"{sharpe} | {wf} | {state} |",
        )

    lines.append("")
    if verdict.triggered_scenarios:
        lines.append("## Triggered reject scenarios")
        lines.append("")
        for s in verdict.triggered_scenarios:
            lines.append(f"- Scenario {s}: " + _SCENARIO_NAMES[s])
        lines.append("")
    if verdict.scenarios_na:
        lines.append("## Scenarios not applicable")
        lines.append("")
        for s in verdict.scenarios_na:
            lines.append(
                f"- Scenario {s}: one side of the comparison "
                "was INSUFFICIENT.",
            )
        lines.append("")

    # Recommended next steps
    lines.append("## Recommended next steps")
    lines.append("")
    if verdict.verdict == "edge_proven":
        lines.append(
            "- Run Phase 3.5.7 sanity audit before claiming the edge.",
        )
        lines.append(
            "- If the audit holds, proceed to Phase 4 (live observer "
            "+ paper trading) per the project roadmap.",
        )
    elif verdict.verdict == "insufficient":
        lines.append(
            "- Extend the backtest window or relax filters to raise the "
            "closed-trade count above the sample-size gate. The "
            "strategy is **not** rejected — it is untested.",
        )
    else:  # rejected
        lines.append(
            "- The strategy as specified does not survive the "
            "falsification framework on this data. Per Phase 3.5 D4, "
            "do not retune thresholds on the same data. Options: "
            "revise the strategy hypothesis (a new sub-phase), enrich "
            "the data (e.g., longer window, additional axes), or end "
            "this strategy track and pivot.",
        )
    lines.append("")
    return "\n".join(lines)


_SCENARIO_NAMES = {
    1: "all four cells Sharpe < 0",
    2: "tier-1 cells positive but tier-2 cells negative (tier-2 universe is not where the edge lives)",
    3: "single-source cells positive but fusion cells negative (fusion layer is not pulling its weight)",
    4: f"best cell Sharpe < {_MIN_BEST_SHARPE} OR walk-forward consistency < {_MIN_BEST_WALK_FORWARD}",
}


def write_verdict_report(
    verdict: FalsificationVerdict,
    results: Sequence[CellRunResult],
    path: Path,
    *,
    period_label: str | None = None,
) -> None:
    """Render + write the verdict report to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_verdict_report(verdict, results, period_label=period_label),
        encoding="utf-8",
    )
    _logger.info(
        "Phase 3.5.6 verdict written to %s: %s",
        path, verdict.verdict,
    )
