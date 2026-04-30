"""Module 37 — Relative Premium Sizing Filter (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class RelativePremiumStage:
    """Module 37 — normalize premium against ticker's 30-day median trade size."""

    name = "m37_relative_premium"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Relative Premium per spec Module 37:
        #   ratio = premium_paid / ticker_30d_median_trade_size
        #     >=3.0 → 1.0
        #     1.5–3.0 → 0.5
        #     <1.5  → 0.0
        # Replaces v4's binary "large premium" flag.
        if event.relative_premium_score is None:
            event.relative_premium_score = 0.5
        return event
