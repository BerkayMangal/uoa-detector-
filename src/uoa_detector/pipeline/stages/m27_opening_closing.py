"""Module 27 — Opening vs. Closing Interest Estimator (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class OpeningClosingStage:
    """Module 27 — estimates whether flow is opening or closing interest."""

    name = "m27_opening_closing"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Opening vs Closing per spec Module 27:
        #   - volume vs OI, new strike activity, repeat same-direction prints
        #   - flag OPENING_UNCONFIRMED when ambiguous; pending next-day OI check.
        # TODO(phase-2): implement Module 28 next-day OI validation scheduler.
        # That validator runs the day after each event and sets
        # event.next_day_oi_confirmed True/False per spec Module 28; for Phase 1
        # we leave it as None unless the synthetic source pre-populates it.
        return event
