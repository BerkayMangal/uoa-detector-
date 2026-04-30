"""Module 26 — Dark Pool / Equity Tape Confirmation (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class DarkPoolStage:
    """Module 26 — confirms options flow against equity dark-pool tape."""

    name = "m26_dark_pool"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Dark Pool confirmation per spec Module 26:
        #   - DP prints below price (bullish absorption) vs above price (bearish distribution)
        #   - drift direction after prints
        #   - set has_dark_pool_confirmation; OPTIONS_EQUITY_TAPE_CONFIRMATION label
        return event
