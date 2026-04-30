"""Module 39 — Time-of-Day Weight (Phase 2: profile-driven).

Window boundaries are exact half-open intervals on the EST/EDT clock.
Conversion uses ``zoneinfo.ZoneInfo("America/New_York")`` which handles DST
transitions correctly (a unit test exercises both March and November
transitions).
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from uoa_detector.calibration.profile import TimeOfDayWeights
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext

_NY = ZoneInfo("America/New_York")

# Half-open windows: [start, end). Order matters — checked top-to-bottom.
# Mirrors the v5 spec table; configurable values come from the profile.
_WINDOW_OPEN_AUCTION = (time(9, 30), time(10, 0))
_WINDOW_EARLY = (time(10, 0), time(11, 0))
_WINDOW_PRIME = (time(11, 0), time(14, 0))
_WINDOW_AFTERNOON = (time(14, 0), time(15, 30))
_WINDOW_MOC_LOC = (time(15, 30), time(16, 0))


def _weight_for(t: time, w: TimeOfDayWeights) -> float:
    """Return the v5 time-of-day weight for the given EST wall-clock time."""
    if _WINDOW_OPEN_AUCTION[0] <= t < _WINDOW_OPEN_AUCTION[1]:
        return w.open_auction
    if _WINDOW_EARLY[0] <= t < _WINDOW_EARLY[1]:
        return w.early_session
    if _WINDOW_PRIME[0] <= t < _WINDOW_PRIME[1]:
        return w.prime
    if _WINDOW_AFTERNOON[0] <= t < _WINDOW_AFTERNOON[1]:
        return w.afternoon
    if _WINDOW_MOC_LOC[0] <= t < _WINDOW_MOC_LOC[1]:
        return w.moc_loc
    # Outside RTH — extended hours and overnight.
    return w.extended_hours


class TimeOfDayStage:
    """Module 39 — apply time-of-day weight per active calibration profile."""

    name = "m39_time_of_day"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        ny_dt = event.print_.timestamp.astimezone(_NY)
        event.time_of_day_weight = _weight_for(ny_dt.time(), ctx.profile.time_of_day)
        return event
