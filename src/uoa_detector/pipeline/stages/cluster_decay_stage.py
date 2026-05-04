"""``ClusterDecayStage`` — event-time pull-based cluster decay check.

Phase 3.1.1: replaces the walltime-driven ``ClusterDecayWatcher`` asyncio
task. Same semantic contract — flips ``cluster_decayed=True`` on the
most-recent event of any buffer that has gone stale — but the clock is
now the **incoming event's own timestamp**, not walltime, so backtest
replay (where walltime collapses to seconds while event-time spans months)
behaves identically to live mode.

Why event-time:
  - In live mode, walltime ≈ event-time, so the old watcher worked fine.
  - In backtest replay, an asyncio task wakes every 60s walltime, which
    might mean "60 seconds in real life" but "0.0001 seconds in the
    replay's event-time window" — the watcher either never fires (if
    walltime is too short to wrap around 60s) or fires at completely
    wrong moments. This stage uses ``incoming_event.print_.timestamp``
    as the decay clock, so decay timing is identical regardless of
    replay speed.

Where it runs:
  - Inserted in ``default_stage_pipeline()`` AFTER ``TemporalClusterStage``
    (M38), so by the time decay check runs, the incoming event has
    already been appended to its cluster buffer and any older buffers
    have a stable "most-recent timestamp" we can compare against.
  - Per-event invocation: every event triggers one decay scan. This is
    cheap because the scan is O(buffers), and ``ctx.cluster_buffers``
    is small (one entry per (ticker, strike, expiry, option_type) seen
    so far).

Idempotency:
  - A buffer already flagged as decayed is skipped (``cluster_decayed``
    bit acts as the latch). The stage can run repeatedly without
    double-counting.

Phase 2 → Phase 3.1.x history:
  The original Phase 2 implementation was a walltime asyncio task
  (``ClusterDecayWatcher``, removed in Phase 3.1.2). Same algorithm,
  same per-buffer decision logic — for each non-empty buffer, take the
  most-recent event, compute age = clock_now - most_recent.timestamp,
  if age >= profile.cluster.decay_minutes and not already flagged then
  flip. The only real difference: ``clock_now`` is now the INCOMING
  event's timestamp, not ``ctx.now()``. In live mode they agreed within
  100ms; in backtest replay they diverged by months. The Phase 3.1.1
  commit included a same-input-same-output equivalence test against
  the old watcher; that test was removed in 3.1.2 along with the
  watcher itself (nothing left to compare against).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext

_logger = structlog.get_logger(__name__)


class ClusterDecayStage:
    """Pull-based cluster decay check, runs once per event.

    Stateless — all decay state lives on the buffered events themselves
    (``cluster_decayed`` flag). The stage just scans, decides, and flips.
    """

    name = "cluster_decay"

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        """Run one decay-detection pass keyed on this event's timestamp.

        Returns the input event unchanged — this stage's effect is on OTHER
        buffered events (it flips their ``cluster_decayed`` flag), not on
        the incoming event itself. The incoming event was just appended to
        its own buffer by M38 immediately upstream; that buffer is fresh by
        definition, so this stage will never flip the incoming event on
        the same pass.
        """
        decay_window = timedelta(
            minutes=ctx.profile.cluster.decay_minutes,
        )
        now = event.print_.timestamp  # event-time clock — the key change

        for key, buf in ctx.cluster_buffers.items():
            if not buf:
                continue
            most_recent = buf[-1]
            # Already flagged on a prior event's pass — leave alone.
            if most_recent.cluster_decayed:
                continue
            age = now - most_recent.print_.timestamp
            if age >= decay_window:
                most_recent.cluster_decayed = True
                _logger.info(
                    "cluster_decayed",
                    ticker=key[0],
                    strike=key[1],
                    expiry=key[2],
                    option_type=key[3],
                    age_seconds=age.total_seconds(),
                    decay_minutes=ctx.profile.cluster.decay_minutes,
                    triggered_by_event_id=event.print_.event_id,
                )

        return event
