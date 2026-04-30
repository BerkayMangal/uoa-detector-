"""Module 39 — Time-of-Day Weight (FULL implementation, per task spec).

Although Module 39 lives in the stages folder, the task spec calls for it to
be implemented correctly because every downstream score depends on it.
Window boundaries are exact half-open intervals on the EST/EDT clock.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from uoa_detector.config import TimeOfDayWeights
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext

_NY = ZoneInfo("America/New_York")

# Half-open windows: [start, end). Order matters — checked top-to-bottom.
# Exactly mirrors the v5 spec table.
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
    # Outside RTH — extended hours and overnight. Spec doesn't define a weight
    # here; we use the lowest published weight (0.30) so off-hours flow can't
    # boost the combined score above what regular-hours noise can.
    return w.open_auction


class TimeOfDayStage:
    """Module 39 — apply time-of-day weight per v5 spec.

    Converts the print's UTC timestamp to America/New_York and looks up the
    weight for the EST wall-clock window:
      09:30–10:00 → 0.30
      10:00–11:00 → 0.80
      11:00–14:00 → 1.00
      14:00–15:30 → 0.70
      15:30–16:00 → 0.30
    """

    name = "m39_time_of_day"

    async def enrich(self, event: EnrichedEvent, ctx: PipelineContext) -> EnrichedEvent:
        ny_dt = event.print_.timestamp.astimezone(_NY)
        event.time_of_day_weight = _weight_for(ny_dt.time(), ctx.config.tod)
        return event
