"""Module 35 — Signal Decay by DTE (Phase 2: profile-driven, fully implemented)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class DTEDecayStage:
    """Module 35 — record the DTE multiplier on the event.

    Per the spec, the multiplier applies ONLY to convexity_score and gamma_score
    sub-components within the combined-score formula. The multiplication itself
    happens inside the scoring engine; this stage records which bucket applies.

    The LEAP rule (DTE > leap_threshold) is enforced here by leaving the
    multiplier valid; the labeler short-circuits LEAPs before scoring matters.
    """

    name = "m35_dte_decay"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        dte = event.print_.dte
        m = ctx.profile.dte
        if dte <= 7:
            event.dte_multiplier_applied = m.bucket_0_7
        elif dte <= 14:
            event.dte_multiplier_applied = m.bucket_8_14
        elif dte <= 30:
            event.dte_multiplier_applied = m.bucket_15_30
        elif dte <= 60:
            event.dte_multiplier_applied = m.bucket_31_60
        else:
            # Includes 61–leap_threshold and >leap_threshold (LEAP). The labeler
            # short-circuits LEAPs before scoring, but we still record the
            # multiplier honestly for the backtest store / decision record.
            event.dte_multiplier_applied = m.bucket_60_plus
        return event
