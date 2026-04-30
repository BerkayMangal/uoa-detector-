"""Module 35 — Signal Decay by DTE (records multiplier; applied in scoring engine)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class DTEDecayStage:
    """Module 35 — record the DTE multiplier on the event.

    Per the spec, the multiplier applies ONLY to convexity_score and gamma_score
    sub-components within the combined-score formula. The actual multiplication
    happens inside the scoring engine; this stage just records which multiplier
    bucket applies, so the backtest store has the value the spec asks for.
    """

    name = "m35_dte_decay"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): if richer DTE handling is needed (e.g., interpolating
        # within buckets, business-day DTE), do it here. Phase 1 picks the bucket
        # straight from the spec table.
        dte = event.print_.dte
        m = ctx.config.dte
        if dte <= 7:
            event.dte_multiplier_applied = m.bucket_0_7
        elif dte <= 14:
            event.dte_multiplier_applied = m.bucket_8_14
        elif dte <= 30:
            event.dte_multiplier_applied = m.bucket_15_30
        elif dte <= 60:
            event.dte_multiplier_applied = m.bucket_31_60
        else:
            # Includes the 61-90 band as well as >90 (LEAP). The labeler
            # short-circuits LEAPs before the scoring formula matters, but we
            # still record the multiplier honestly for the backtest store.
            event.dte_multiplier_applied = m.bucket_60_plus
        return event
