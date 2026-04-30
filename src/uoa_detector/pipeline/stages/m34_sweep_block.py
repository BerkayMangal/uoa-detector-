"""Module 34 — Sweep vs. Block Order Classifier (stub)."""

from __future__ import annotations

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class SweepBlockStage:
    """Module 34 — classify each print as block / sweep / ISO."""

    name = "m34_sweep_block"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        # TODO(phase-2): implement Sweep/Block classification per spec Module 34:
        #   - block: single print, single exchange (lower conviction)
        #   - sweep: multi-exchange within ms (higher conviction; +0.10 to uoa)
        #   - ISO: feed-tagged (highest urgency; +0.15 to uoa, SWEEP_UOA label)
        # Phase 1: trust the print's is_iso flag, default everything else to "block".
        if event.sweep_classification is None:
            event.sweep_classification = "iso" if event.print_.is_iso else "block"
        if event.uoa_score is None:
            event.uoa_score = 0.5
        return event
