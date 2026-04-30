"""Module 21 — Dealer Gamma Overlay (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class DealerGammaStage:
    """Module 21 — adds dealer-positioning context to every signal."""

    name = "m21_dealer_gamma"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Dealer Gamma Overlay per spec Module 21:
        #   - compute gamma_score from spot/strike proximity, DTE, OI, IV
        #   - flag GAMMA_ACCELERATION_RISK when DTE<=14, strike within 5-10% spot,
        #     large OI at strike, aggressive buying near strike, spot approaching.
        if event.gamma_score is None:
            event.gamma_score = 0.5
        # is_gamma_acceleration left at default False; labeler treats absence as no risk.
        return event
