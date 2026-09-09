"""Daily screener digest — Phase 3.6.

Renders a ranked, human-readable *candidate funnel* from a batch of
``SignalDecisionRecord``s produced by the full pipeline (UW + ThetaData
enrichment, M21-M28). This is **decision-support**, not proven edge: the
ranking is by the pipeline's existing confluence (combined) score, and the
digest adds no new scoring and no new thresholds (D8).

The module is pure rendering:

  - ``screen_records``  — filter to actionable candidates + deterministic rank.
  - ``render_stdout``   — fixed-width terminal table.
  - ``render_markdown`` — GitHub-flavoured markdown twin for ``--report-path``.

Both renderers are pure functions of their input, so a given batch of
records always produces byte-identical output (determinism requirement).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from uoa_detector.domain.labels import SignalLabel

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from uoa_detector.observability.decision_record import SignalDecisionRecord

# Verbatim intent header — pinned by the Phase 3.6 acceptance doc (§2).
DIGEST_INTENT = (
    "Decision-support candidates — ranked by confluence score. "
    "Apply your own judgment; these are NOT proven-profitable signals."
)

# Legend making the SIDE column's meaning explicit (acceptance doc §4.1):
# the detector only surfaces option-buying flow, so SIDE is always LONG
# (long the option). Bullish vs bearish is the call/put in CONTRACT.
DIGEST_LEGEND = (
    "LONG = long the option contract (long premium / long convexity). "
    "Bullish vs bearish view = call (C) / put (P) in CONTRACT."
)

EMPTY_MESSAGE = "No candidates cleared thresholds today."

# The trade side is always LONG the option contract — see acceptance §4.1.
LONG_SIDE = "LONG"

# Noise / not-evaluated labels: never actionable regardless of sized R.
# Belt-and-suspenders with the ``max_r > 0`` filter (acceptance doc §3).
NON_ACTIONABLE_LABELS: frozenset[SignalLabel] = frozenset(
    {
        SignalLabel.IGNORE_NOISE,
        SignalLabel.LIKELY_CLOSING_OR_NOISE,
        SignalLabel.POST_EVENT_NOISE,
        SignalLabel.PENALIZED_BELOW_THRESHOLD,
        SignalLabel.REJECTED,
    },
)


@dataclass(frozen=True)
class DigestRow:
    """One rendered candidate line. Frozen — the digest is an audit view."""

    rank: int
    ticker: str
    side: str
    label: str
    score: float
    contract: str
    max_r: float
    reasons: tuple[str, ...]
    label_reason: str


def _combined_score(record: SignalDecisionRecord) -> float:
    """The pipeline's final post-penalty combined score for ranking.

    Actionable candidates always have this set; a defensive ``None`` sorts
    to the bottom rather than raising.
    """
    score = record.event.combined_score_post_penalty
    return float(score) if score is not None else float("-inf")


def is_actionable(record: SignalDecisionRecord) -> bool:
    """A record a trader could act on: positioned and not a noise label."""
    return (
        record.size.max_r > 0.0
        and record.decision.label not in NON_ACTIONABLE_LABELS
    )


def _sort_key(
    record: SignalDecisionRecord,
) -> tuple[float, str, str, Decimal, str, str]:
    """Deterministic rank key: score desc, then a stable identity tiebreak."""
    pr = record.event.print_
    return (
        -_combined_score(record),
        pr.ticker,
        pr.option_type,
        pr.strike,
        pr.expiry.isoformat(),
        pr.event_id,
    )


def _fmt_strike(strike: Decimal) -> str:
    """Render an integral strike as ``250``; keep the tail otherwise."""
    if strike == strike.to_integral_value():
        return str(int(strike))
    return format(strike.normalize(), "f")


def _fmt_contract(record: SignalDecisionRecord) -> str:
    """``C 250 @ 2026-07-18 (7DTE)`` from the underlying print."""
    pr = record.event.print_
    letter = "C" if pr.option_type == "call" else "P"
    return (
        f"{letter} {_fmt_strike(pr.strike)} @ "
        f"{pr.expiry.isoformat()} ({pr.dte}DTE)"
    )


def _top_reasons(record: SignalDecisionRecord, *, limit: int = 2) -> tuple[str, ...]:
    """Top-``limit`` positive score-breakdown contributors, ``name +0.42``.

    Penalty entries (``penalty_*``) are excluded — reasons explain why a
    candidate ranked, not what dragged it down. Ordered by contribution
    descending with a name tiebreak for determinism.
    """
    components = [
        (name, value)
        for name, value in record.score_breakdown.items()
        if not name.startswith("penalty_")
    ]
    components.sort(key=lambda kv: (-kv[1], kv[0]))
    return tuple(f"{name} {value:+.2f}" for name, value in components[:limit])


def screen_records(records: Iterable[SignalDecisionRecord]) -> list[DigestRow]:
    """Filter to actionable candidates and rank them deterministically."""
    actionable = [r for r in records if is_actionable(r)]
    actionable.sort(key=_sort_key)
    return [
        DigestRow(
            rank=i + 1,
            ticker=r.event.print_.ticker,
            side=LONG_SIDE,
            label=r.decision.label.value,
            score=_combined_score(r),
            contract=_fmt_contract(r),
            max_r=r.size.max_r,
            reasons=_top_reasons(r),
            label_reason=r.decision.reason,
        )
        for i, r in enumerate(actionable)
    ]


_HEADERS = ("#", "TICKER", "SIDE", "LABEL", "SCORE", "CONTRACT", "MAX_R", "TOP REASONS")


def _meta_line(*, profile_id: str, run_timestamp: datetime, count: int) -> str:
    return (
        f"profile: {profile_id} | run: {run_timestamp.isoformat()} | "
        f"candidates: {count}"
    )


def render_stdout(
    rows: Sequence[DigestRow],
    *,
    profile_id: str,
    run_timestamp: datetime,
) -> str:
    """Fixed-width terminal digest. Deterministic for a given input."""
    lines = [DIGEST_INTENT, _meta_line(profile_id=profile_id, run_timestamp=run_timestamp, count=len(rows))]
    if not rows:
        lines.append("")
        lines.append(EMPTY_MESSAGE)
        return "\n".join(lines) + "\n"

    cells: list[tuple[str, ...]] = [_HEADERS]
    for row in rows:
        cells.append(
            (
                str(row.rank),
                row.ticker,
                row.side,
                row.label,
                f"{row.score:.3f}",
                row.contract,
                f"{row.max_r:.2f}",
                ", ".join(row.reasons),
            ),
        )
    widths = [max(len(c[i]) for c in cells) for i in range(len(_HEADERS))]

    def _fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols)).rstrip()

    lines.append("")
    lines.append(_fmt(cells[0]))
    lines.append("  ".join("-" * widths[i] for i in range(len(_HEADERS))))
    lines.extend(_fmt(c) for c in cells[1:])
    lines.append("")
    lines.append(DIGEST_LEGEND)
    return "\n".join(lines) + "\n"


def render_markdown(
    rows: Sequence[DigestRow],
    *,
    profile_id: str,
    run_timestamp: datetime,
) -> str:
    """GitHub-flavoured markdown twin for ``--report-path``."""
    lines = [
        "# Screener digest",
        "",
        f"> {DIGEST_INTENT}",
        "",
        _meta_line(profile_id=profile_id, run_timestamp=run_timestamp, count=len(rows)),
        "",
    ]
    if not rows:
        lines.append(EMPTY_MESSAGE)
        return "\n".join(lines) + "\n"

    lines.append(
        "| # | Ticker | Side | Label | Score | Contract | Max R | Top reasons | Labeler note |",
    )
    lines.append("|--:|---|---|---|--:|---|--:|---|---|")
    for row in rows:
        reasons = ", ".join(row.reasons)
        lines.append(
            f"| {row.rank} | {row.ticker} | {row.side} | {row.label} | "
            f"{row.score:.3f} | {row.contract} | {row.max_r:.2f} | "
            f"{reasons} | {row.label_reason} |",
        )
    lines.append("")
    lines.append(f"_{DIGEST_LEGEND}_")
    return "\n".join(lines) + "\n"
