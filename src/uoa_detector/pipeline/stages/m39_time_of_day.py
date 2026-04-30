"""Module 39 — Time-of-Day Weight (Phase 2: fully profile-driven).

Window boundaries, weights, labels, and timezone all come from
``profile.time_of_day``. This file contains zero numerical or string-time
literals.

DST is handled via ``zoneinfo``: converting a UTC timestamp to the configured
timezone respects the offset that applies on that wall-clock date, so the
windows map to the correct local time on both sides of any transition.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext


class TimeOfDayStage:
    """Module 39 — apply time-of-day weight per the active calibration profile."""

    name = "m39_time_of_day"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        tod = ctx.profile.time_of_day
        local_dt = event.print_.timestamp.astimezone(ZoneInfo(tod.timezone))
        weight, label = tod.lookup(local_dt.time())
        event.time_of_day_weight = weight
        event.time_window_label = label
        return event
