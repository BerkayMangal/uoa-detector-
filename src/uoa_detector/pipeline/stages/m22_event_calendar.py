"""Module 22 — Event Calendar Overlay (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class EventCalendarStage:
    """Module 22 — checks every signal against upcoming catalyst timing."""

    name = "m22_event_calendar"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Event Calendar Overlay per spec Module 22:
        #   - look up earnings, FDA, Fed, M&A dates per ticker
        #   - produce event_score: 0.00 (no event), 0.50 (<=30d), 0.75 (<=7d), 1.00 (<=3d high-vol)
        #   - upgrade label to PRE_CATALYST_FLOW where conditions match
        if event.event_score is None:
            event.event_score = 0.5
        return event
