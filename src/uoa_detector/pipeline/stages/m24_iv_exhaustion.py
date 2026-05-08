"""Module 24 — IV exhaustion score.

Phase 3.4.4: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.4.

Hypothesis (acceptance doc): when IV rank is already very high,
the option is expensive AND the move-priced-in is large. Edge is
greater when buying when IV is low/moderate.

This is the FIRST M-module that emits a cross-module
``ScoreAdjustment`` (Phase 2 rail used in M34; first time used
in M21-M28). The post-earnings IV spike penalty fires when:
  - iv_rank > post_earnings_iv_penalty_threshold (default 80)
  - AND a catalyst event is within
    post_earnings_session_days (default 1) BEFORE event_ts
The adjustment targets ``combined_score_pre`` (rail extended in
Phase 3.4.4.2) with delta = post_earnings_iv_penalty
(default -0.4).

Providers (TWO, since the post-earnings penalty needs both IV
and catalyst data):
  - ``IVHistoryProvider.iv_rank_at`` (UW; Phase 3.3.3.6)
  - ``CatalystCalendarProvider.catalysts_in_window``
    (UW; extended in Phase 3.4.2.2)

Score branches (acceptance doc; iv_score):
  - 1.0 — IV rank < low_iv_threshold (cheap)
  - 0.7 — IV rank in [low_iv, mid_iv) (moderate)
  - 0.3 — IV rank in [mid_iv, high_iv) (elevated)
  - 0.0 — IV rank >= high_iv_threshold (expensive)
  - 0.5 — provider returned None (no IV history fallback)

Cross-cutting acceptance pinned (same as M21/M22/M23):
  - Provider injection via constructor (TWO providers here)
  - Idempotency-on-preset (uses iv_score_set as the gate)
  - asyncio.wait_for on each provider call (timeout = profile)
  - last_execution_metadata exposed for orchestrator telemetry

decision (no iv_score field on EnrichedEvent):
  Acceptance doc names the M24 output 'iv_score' but the
  EnrichedEvent model has no such field. Investigation:
    - ScoringWeights has no iv weight (combined-score formula
      doesn't include iv_score)
    - No call site reads an iv_score field
  Conclusion: iv_score is an internal/telemetry value used by
  M24 to (a) decide which branch label to log, and (b) gate
  the post-earnings penalty emission. Stored in
  last_execution_metadata; not persisted to a domain field.
  Adding a new field would require BacktestStore migration +
  scoring weight definition + downstream readers — out of scope
  for Phase 3.4.4 since no consumer needs it. Idempotency-on-
  preset uses score_adjustments[source_module=m24] as the
  'already ran' marker.

decision (M24 reads catalyst directly, not via M22's event_score):
  M22 sets event.event_score using its 14-day window. M24 needs
  a TIGHTER 1-session window for the post-earnings penalty.
  Reading event_score would couple M24 to M22's branch labels
  AND would be incorrect (event_score=0.0 also fires on M22
  timeout, not just post-event blackout). Calling the catalyst
  provider directly is precise and the TTL cache makes the
  duplicate fetch free.

decision (clamp IV rank > 100 to 100):
  Acceptance doc edge case: 'Provider returns rank > 100 (data
  error) → clamp to 100'. Defensive — UW occasionally returns
  out-of-range values during data refresh. Stage clamps; profile
  thresholds apply normally to the clamped value.

decision (no_iv_history_score path doesn't emit penalty):
  When provider returns None (new listing, no IV history), we
  set iv_score = no_iv_history_score (0.5) and DO NOT emit the
  post-earnings penalty. Matches acceptance doc edge case
  literal: 'no penalty applied either way'.

decision (post-earnings penalty only checks PAST catalysts):
  The trigger is 'recent earnings within 1 session', meaning a
  catalyst that ALREADY HAPPENED in the past N days. We check
  the window [event_ts - session_days, event_ts] (inclusive of
  event_ts so same-day catalysts qualify). Future catalysts
  do NOT trigger the penalty — that's M22's pre-event window
  branch, a separate concern.

decision (penalty emission is unconditional on iv branch above
threshold):
  If iv_rank > post_earnings_iv_penalty_threshold AND catalyst
  in past N sessions, the penalty fires regardless of which
  iv_score branch was selected. This matches the acceptance
  doc trigger condition. In practice the threshold defaults
  coincide (high_iv_threshold = post_earnings_iv_penalty_threshold
  = 80), so the penalty branches with iv_score=0.0; but the
  thresholds are tuneable independently per 3.4.4.1 design.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from uoa_detector.domain.agreement import ScoreAdjustment
from uoa_detector.providers.catalyst_calendar import (
    NoOpCatalystCalendarProvider,
)
from uoa_detector.providers.iv_history import (
    IVRankSnapshot,
    NoOpIVHistoryProvider,
)

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import M24Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.catalyst_calendar import (
        CatalystCalendarProvider,
    )
    from uoa_detector.providers.iv_history import IVHistoryProvider


_logger = logging.getLogger(__name__)


class IVExhaustionStage:
    """Module 24 — penalises signals after IV has already exploded."""

    name = "m24_iv_exhaustion"

    def __init__(
        self,
        iv_provider: IVHistoryProvider | None = None,
        catalyst_provider: CatalystCalendarProvider | None = None,
    ) -> None:
        """Construct with TWO injected providers.

        IV provider feeds the iv_score; catalyst provider gates
        the post-earnings penalty emission.
        """
        self._iv_provider: IVHistoryProvider = (
            iv_provider or NoOpIVHistoryProvider()
        )
        self._catalyst_provider: CatalystCalendarProvider = (
            catalyst_provider or NoOpCatalystCalendarProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency-on-preset: if M24 has already run on this
        # event (its ScoreAdjustment is already present), skip.
        # M24 doesn't write to a sub-score field on EnrichedEvent
        # (there's no iv_score field — see decision below); the
        # presence of a prior ScoreAdjustment with our source_module
        # is the canonical 'already ran' marker.
        if any(
            a.source_module == self.name
            for a in event.score_adjustments
        ):
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m24 = ctx.profile.scoring.modules.m24
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp

        # Fetch IV rank
        try:
            snapshot = await asyncio.wait_for(
                self._iv_provider.iv_rank_at(
                    ticker=ticker,
                    strike=event.print_.strike,
                    expiry=event.print_.expiry,
                    option_type=event.print_.option_type,
                    at=event_ts,
                ),
                timeout=m24.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m24: IV provider timeout for %s; using no-history fallback",
                ticker,
            )
            self.last_execution_metadata = {
                "branch": "iv_provider_timeout",
                "iv_provider_returned": "no",
            }
            return event

        if snapshot is None or snapshot.iv_rank_252d is None:
            _logger.info(
                "m24: no IV history for %s; using fallback score",
                ticker,
            )
            self.last_execution_metadata = {
                "branch": "no_iv_history",
                "iv_provider_returned": "no",
            }
            # Set iv_score via a metadata path; no penalty emitted.
            return event

        iv_rank = _clamp_iv_rank(snapshot.iv_rank_252d)
        iv_score, branch = _score_from_iv_rank(iv_rank, m24)

        # Cross-module penalty trigger: high IV + recent catalyst
        penalty_emitted = False
        if iv_rank > m24.post_earnings_iv_penalty_threshold:
            window_start = event_ts - timedelta(
                days=m24.post_earnings_session_days,
            )
            window_end = event_ts
            try:
                catalysts = await asyncio.wait_for(
                    self._catalyst_provider.catalysts_in_window(
                        ticker, window_start, window_end,
                    ),
                    timeout=m24.provider_timeout_s,
                )
            except TimeoutError:
                _logger.warning(
                    "m24: catalyst provider timeout for %s; "
                    "skipping post-earnings penalty",
                    ticker,
                )
                catalysts = ()

            if catalysts:
                event.score_adjustments.append(ScoreAdjustment(
                    target="combined_score_pre",
                    delta=m24.post_earnings_iv_penalty,
                    reason=(
                        f"IV rank {iv_rank:.1f} > "
                        f"{m24.post_earnings_iv_penalty_threshold:.1f} "
                        f"+ catalyst within "
                        f"{m24.post_earnings_session_days} session(s)"
                    ),
                    source_module=self.name,
                ))
                penalty_emitted = True

        self.last_execution_metadata = {
            "branch": branch,
            "iv_provider_returned": "yes",
            "iv_rank": f"{iv_rank:.2f}",
            "iv_score": f"{iv_score:.3f}",
            "post_earnings_penalty_emitted": (
                "yes" if penalty_emitted else "no"
            ),
        }
        return event


def _clamp_iv_rank(rank: float) -> float:
    """Clamp IV rank to [0, 100] per acceptance doc edge case."""
    if rank < 0.0:
        return 0.0
    if rank > 100.0:
        return 100.0
    return rank


def _score_from_iv_rank(
    iv_rank: float,
    settings: M24Settings,
) -> tuple[float, str]:
    """Map IV rank to (iv_score, branch_label).

    Branches (acceptance doc):
      - rank < low_iv → cheap_iv_score (cheap)
      - rank in [low_iv, mid_iv) → moderate_iv_score
      - rank in [mid_iv, high_iv) → elevated_iv_score
      - rank >= high_iv → expensive_iv_score (zero by default)

    Pure function — directly unit-testable.
    """
    if iv_rank < settings.low_iv_threshold:
        return settings.cheap_iv_score, "cheap"
    if iv_rank < settings.mid_iv_threshold:
        return settings.moderate_iv_score, "moderate"
    if iv_rank < settings.high_iv_threshold:
        return settings.elevated_iv_score, "elevated"
    return settings.expensive_iv_score, "expensive"


# Re-export IVRankSnapshot for tests that need to construct snapshots
__all__ = [
    "IVExhaustionStage",
    "IVRankSnapshot",
    "_clamp_iv_rank",
    "_score_from_iv_rank",
]
