"""Module 23 — Price Confirmation Layer (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class PriceConfirmationStage:
    """Module 23 — flow must be confirmed by price action."""

    name = "m23_price_confirmation"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Price Confirmation per spec Module 23:
        #   - VWAP hold/loss, HH/HL vs LH/LL, prior-day range break, volume expansion
        #   - set price_confirmation_score in [0,1] and flip has_price_confirmation
        #   - set price_direction in {"up","down","neutral"} so the contradiction
        #     penalty in the scoring engine has data to work with.
        if event.price_confirmation_score is None:
            event.price_confirmation_score = 0.5
        if event.price_direction is None:
            event.price_direction = "neutral"
        return event
