"""``SignalDecisionRecord`` — full per-event audit record.

Phase 2 acceptance criterion: every event that completes the pipeline
must produce a structured decision record suitable for downstream
analysis (jq, pandas, dashboards). The record carries enough
information that, given a stored record and the historical profile YAML,
the operator can reconstruct exactly why a signal got the label it did.

Contents (per the prompt):

  - The full ``OptionsPrint`` (already includes ``source_agreement``).
  - The full ``EnrichedEvent`` with all sub-scores, score adjustments,
    flags, applied penalties, missing sub-score list, etc.
  - The ``CalibrationProfile.profile_id`` and a SHA-256 of the profile's
    content so reproducibility doesn't depend on filesystem state.
  - Per-stage execution log: list of ``StageExecutionEntry`` with
    ``stage_name``, ``latency_ms``, and ``notes``. Latency is measured
    in walltime per ``time.perf_counter()``.
  - Penalty trace (``event.applied_penalties``).
  - Score breakdown (``score_breakdown(event, profile)``) — per-component
    weighted contributions, plus ``penalty_<name>`` entries.
  - The ``LabelDecision`` (label + reasoning).
  - The ``PositionSize`` (max_r, scale_in, initial_r).
  - ``decision_emitted_at`` walltime so analysts can correlate with
    real-time logs.

The record is a frozen Pydantic model so once constructed, downstream
mutation is impossible — the audit trail can be trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision
from uoa_detector.domain.risk import PositionSize
from uoa_detector.scoring.combined import score_breakdown

if TYPE_CHECKING:
    from uoa_detector.calibration import CalibrationProfile


class StageExecutionEntry(BaseModel):
    """One stage's run on one event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_name: str
    latency_ms: float = Field(ge=0.0)
    notes: str | None = None


class SignalDecisionRecord(BaseModel):
    """Full audit record for one fully-processed signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity / when
    decision_emitted_at: datetime  # tz-aware UTC; walltime when record was built
    profile_id: str
    profile_content_hash: str

    # Full event state at decision time. EnrichedEvent contains the
    # OptionsPrint via .print_, sub-scores, score adjustments, flags,
    # penalties applied, missing sub-scores, and (Phase 2.3.1+) the
    # rejection record if any.
    event: EnrichedEvent

    # Per-stage execution trace
    stage_executions: list[StageExecutionEntry] = Field(default_factory=list)

    # Score decomposition. Components sum to combined_score_pre_penalty;
    # penalty_<name> entries are negative deltas applied post-component-sum.
    score_breakdown: dict[str, float]

    # Final label and risk sizing.
    decision: LabelDecision
    size: PositionSize


def build_decision_record(
    *,
    event: EnrichedEvent,
    profile: CalibrationProfile,
    decision: LabelDecision,
    size: PositionSize,
    stage_executions: list[StageExecutionEntry] | None = None,
    emitted_at: datetime | None = None,
) -> SignalDecisionRecord:
    """Construct a ``SignalDecisionRecord`` from the pieces the orchestrator
    has at hand at the end of ``process_one``.

    Computes the score breakdown via ``scoring.combined.score_breakdown``;
    captures the profile's content hash via ``profile.content_hash()``.
    """
    return SignalDecisionRecord(
        decision_emitted_at=emitted_at or datetime.now(tz=UTC),
        profile_id=profile.profile_id,
        profile_content_hash=profile.content_hash(),
        event=event,
        stage_executions=list(stage_executions or ()),
        score_breakdown=score_breakdown(event, profile),
        decision=decision,
        size=size,
    )
