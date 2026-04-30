"""Module 24 — IV Exhaustion Filter (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class IVExhaustionStage:
    """Module 24 — penalize signals after IV has already exploded."""

    name = "m24_iv_exhaustion"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement IV Exhaustion per spec Module 24:
        #   - check IV change vs baseline, option repricing magnitude, spread widening
        #   - feeds the penalty engine for IV-rank and post-event conditions
        return event
