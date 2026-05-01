"""``ClusterDecayWatcher`` — sets ``cluster_decayed=True`` on stale cluster buffers.

Phase 2 / Module 38 follow-up. The temporal-clustering stage builds up
per-(ticker, strike, expiry, option_type) buffers as flow events arrive.
A buffer that has not received a new qualifying print within
``profile.cluster.decay_minutes`` is "stale" — the cluster has decayed.
When that happens, the labeler downgrades a CONVEXITY_CLUSTER signal to
CONVEXITY_WATCH on subsequent labeling passes for that key, because the
event's ``cluster_decayed`` flag will be True.

# TODO(phase-3): refactor to pull-based decay check using
# event.print.timestamp as the clock; remove the asyncio task and the
# walltime-driven check_interval_s loop entirely.
#
# Current design is walltime-driven: the asyncio task wakes every
# check_interval_s walltime seconds and asks "is now - most_recent.event_time
# >= decay_minutes?". This works in live mode (walltime ~ event-time) and
# in unit tests (which inject ctx.clock to fake walltime). It breaks in
# backtest replay where walltime collapses to seconds while event-time
# spans months — the asyncio task either never fires or fires at the
# wrong moments.
#
# Phase 3 should turn this into a stage that runs on every event and
# checks decay using event.print.timestamp as the clock — no background
# task, no walltime dependency, identical behaviour in live and replay.
# The decay_once() method already does the right thing if called with
# ctx.clock returning event-time; the asyncio loop is the part that
# doesn't generalise. Keep the unit tests (they pin ctx.clock to a
# specific datetime, so they'd survive the refactor) and rewrite the
# orchestrator wiring to be a stage rather than a create_task.

Design (Phase 2)
----------------
The watcher is a periodic asyncio task. Every ``check_interval_s`` walltime
seconds it scans all open ``ctx.cluster_buffers`` and, for each buffer
whose most-recent event's timestamp (event-time) is older than the
configured decay window relative to the active clock (``ctx.now()``), sets
``cluster_decayed = True`` on the most-recent buffered event.

Only that single most-recent event gets the flag — the older events in
the same buffer were already past the decay window when they were stored
(if they weren't, they'd still be active). The flag on the most-recent
event is what the labeler reads if the event is re-scored later.

Walltime vs event-time. Stall detection here uses the active clock for
the "is this stale now" question, but the timestamp comparison is against
``event.print_.timestamp`` (event-time) because that's how M38's window
logic works. In a backtest where the clock runs faster than walltime,
inject ``ctx.clock`` to an event-time-driven function so decay timing
matches what M38 actually observed.

Tests / non-asyncio entry point. ``decay_once()`` performs one scan-and-
flag pass synchronously (no asyncio sleep). Tests use this directly to
avoid scheduling a real timer; the asyncio task ``run()`` simply calls
``decay_once()`` in a loop with ``asyncio.sleep(check_interval_s)``
between passes.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from uoa_detector.pipeline.stage import PipelineContext

_logger = structlog.get_logger(__name__)


class ClusterDecayWatcher:
    """Periodic scanner that flags stale cluster buffers as decayed.

    Construct with ``PipelineContext`` (read-only — the watcher mutates
    only the ``cluster_decayed`` flag on already-buffered events) and an
    optional ``check_interval_s`` walltime cadence. Call ``run()`` to
    enter the asyncio loop, or ``decay_once()`` for a single sync pass.
    """

    def __init__(
        self,
        ctx: PipelineContext,
        *,
        check_interval_s: float = 60.0,
    ) -> None:
        self._ctx = ctx
        self._check_interval_s = check_interval_s
        self._stop = asyncio.Event()

    def decay_once(self) -> int:
        """Run one decay-detection pass synchronously. Returns the number of
        buffers that newly transitioned to decayed in this pass.

        A buffer is "newly transitioned" if its most-recent event's
        ``cluster_decayed`` flag was False at entry and is True at exit.
        Buffers that were already flagged from a prior pass are not counted.
        """
        decay_window = timedelta(
            minutes=self._ctx.profile.cluster.decay_minutes,
        )
        now = self._ctx.now()
        newly_decayed = 0

        for key, buf in self._ctx.cluster_buffers.items():
            if not buf:
                continue
            most_recent = buf[-1]
            # Already flagged in a prior pass — leave alone.
            if most_recent.cluster_decayed:
                continue
            age = now - most_recent.print_.timestamp
            if age >= decay_window:
                most_recent.cluster_decayed = True
                newly_decayed += 1
                _logger.info(
                    "cluster_decayed",
                    ticker=key[0],
                    strike=key[1],
                    expiry=key[2],
                    option_type=key[3],
                    age_seconds=age.total_seconds(),
                    decay_minutes=self._ctx.profile.cluster.decay_minutes,
                )

        return newly_decayed

    async def run(self) -> None:
        """Run the periodic decay-check loop until ``stop()`` is called.

        Each iteration:
          1. Performs one ``decay_once()`` scan.
          2. Sleeps ``check_interval_s`` seconds (cancellable via stop event).

        Cancellation is exception-safe — ``asyncio.CancelledError`` is
        propagated rather than suppressed so the orchestrator can drain
        the task cleanly.
        """
        while not self._stop.is_set():
            self.decay_once()
            # Wait either for the stop signal or the timeout (whichever first).
            # TimeoutError is the normal-cadence path and is suppressed; any
            # other exception (including CancelledError) propagates.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._check_interval_s,
                )

    def stop(self) -> None:
        """Signal the run loop to exit at its next wakeup."""
        self._stop.set()
