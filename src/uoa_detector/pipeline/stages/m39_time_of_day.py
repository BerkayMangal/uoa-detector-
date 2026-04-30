"""Module 39 — Time-of-Day Weight (Phase 2: fully profile-driven).

Window boundaries, weights, labels, timezone, and the extended-hours policy
all come from ``profile.time_of_day``. This file contains zero numerical or
string-time literals.

Policies for outside-session prints (see ``ExtendedHoursPolicy``):
  - ``"weight"``: apply ``outside_session_weight`` silently (Phase 1 behavior).
  - ``"flag"``: apply ``outside_session_weight`` AND set
    ``event.flags["extended_hours"] = True`` so downstream filtering is easy.
  - ``"reject"``: drop the print — set ``event.rejection`` and skip the rest
    of the pipeline. The orchestrator detects this and short-circuits.

DST is handled via ``zoneinfo``: converting a UTC timestamp to the configured
timezone respects the offset that applies on that wall-clock date, so the
windows map to the correct local time on both sides of any transition.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.rejection import RejectedEvent
from uoa_detector.pipeline.stage import PipelineContext

# String literal extracted to a constant so the M39 source has no string-time
# or string-policy literals scattered through control flow. Stage names are
# identity tokens, not threshold values.
_OUTSIDE_SESSION_LABEL = "outside_session"
_EXTENDED_HOURS_FLAG = "extended_hours"
_STAGE_NAME = "m39_time_of_day"


class TimeOfDayStage:
    """Module 39 — apply time-of-day weight per the active calibration profile."""

    name = _STAGE_NAME

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        tod = ctx.profile.time_of_day
        local_dt = event.print_.timestamp.astimezone(ZoneInfo(tod.timezone))
        weight, label = tod.lookup(local_dt.time())

        outside_session = label == _OUTSIDE_SESSION_LABEL

        if outside_session and tod.extended_hours_policy == "reject":
            # Drop the print: do NOT set time_of_day_weight. The orchestrator
            # sees event.rejection != None and skips remaining stages, scoring,
            # penalties, labeling, and sizing.
            event.rejection = RejectedEvent(
                reason=f"{_EXTENDED_HOURS_FLAG}: extended_hours_policy='reject'",
                rejected_by_stage=self.name,
                rejected_at=ctx.now(),
            )
            return event

        # "weight" and "flag" both apply the weight; "flag" additionally tags.
        event.time_of_day_weight = weight
        event.time_window_label = label

        if outside_session and tod.extended_hours_policy == "flag":
            event.flags[_EXTENDED_HOURS_FLAG] = True

        return event
