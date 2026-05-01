"""Module 38 — Temporal Clustering & Convexity Cluster Detection.

Maintains a per-(ticker, strike, expiry, option_type) buffer of recent
events in ``PipelineContext.cluster_buffers``. For each incoming event,
appends to the matching buffer and computes ``cluster_density_score``
based on how many recent prints fall in the rolling window plus whether
they show size escalation or span nearby strikes.

Density bands (from ``profile.cluster``, ranked highest first):

  1. **Three-or-more same strike/expiry, escalating size** → ``density_three_escalating``
     (1.0 in v5). Each successive print's premium is ≥
     ``profile.cluster.escalation_ratio`` × the previous one. Signals an
     accelerating buyer who's willing to pay more as urgency builds.
     Also sets ``event.escalating_cluster = True``.

  2. **Three-or-more same strike/expiry** (no escalation) → ``density_three_same``
     (0.8 in v5).

  3. **Two same strike/expiry** → ``density_two_same`` (0.4 in v5).

  4. **Three-or-more across nearby strikes, same expiry** →
     ``density_nearby_strikes`` (0.6 in v5). "Nearby" = within
     ``profile.cluster.nearby_strike_count`` chain-grid steps. The 0.6
     band sits between two-same (0.4) and three-same (0.8) because it's
     a softer signal — multiple strikes suggests the buyer is hedging
     across the chain rather than concentrating conviction.

  5. **Single print** (or anything that doesn't qualify above) →
     ``density_isolated`` (0.0 in v5).

Window: only events with ``timestamp >= now - profile.cluster.window_minutes``
count toward density. Older entries stay in the buffer (so M38 doesn't
have to evict eagerly) but are filtered out at scoring time.

Idempotency: appending the same event twice would double-count it. The
stage skips the append (but still recomputes the density) if the
event_id is already in the buffer. This makes the stage safe to re-run
during pipeline replay or manual re-enrichment.

ClusterDecayWatcher (NOT in this stage — separate background task):
scans buffers periodically; when no new qualifying print has arrived
within ``profile.cluster.decay_minutes``, sets ``cluster_decayed=True``
on the most recent event from that buffer so the labeler downgrades
``CONVEXITY_CLUSTER`` → ``CONVEXITY_WATCH``. Implemented in a follow-up
commit alongside its orchestrator wire-up.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import (
    ClusterKey,
    PipelineContext,
    cluster_key,
)


class TemporalClusterStage:
    """Module 38 — score temporal clustering and detect size escalation."""

    name = "m38_temporal_cluster"

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Always update the buffer first, even if scoring is skipped — the
        # buffer is part of the per-event audit trail and downstream events
        # in the same scenario need to see this print's history.
        key = cluster_key(event)
        buf = ctx.cluster_buffers.setdefault(key, deque(maxlen=1024))
        already_in_buf = any(
            e.print_.event_id == event.print_.event_id for e in buf
        )
        if not already_in_buf:
            buf.append(event)

        # Idempotency: do not overwrite a score already populated upstream
        # (typically the scenario override stage). The escalating_cluster
        # flag is similarly preserved.
        if event.cluster_density_score is not None:
            return event

        params = ctx.profile.cluster
        window = timedelta(minutes=params.window_minutes)

        # Same-strike events in the rolling window (chronological).
        same_recent = self._recent_in_window(buf, event, window)

        # Detect escalation: 3+ events with each premium >= escalation_ratio
        # times the previous. Sorted by event-time so the chain is meaningful.
        is_escalating = self._is_escalating(same_recent, params.escalation_ratio)

        # Apply same-strike bands first (they outrank nearby-strike bands).
        if len(same_recent) >= 3 and is_escalating:
            event.cluster_density_score = params.density_three_escalating
            event.escalating_cluster = True
            return event
        if len(same_recent) >= 3:
            event.cluster_density_score = params.density_three_same
            return event
        if len(same_recent) >= 2:
            event.cluster_density_score = params.density_two_same
            return event

        # Single same-strike print — fall back to nearby-strike band if
        # qualifying activity exists at adjacent strikes.
        nearby_recent_count = self._nearby_strike_count(
            ctx.cluster_buffers,
            event,
            params.nearby_strike_count,
            window,
        )
        if nearby_recent_count >= 3:
            event.cluster_density_score = params.density_nearby_strikes
            return event

        event.cluster_density_score = params.density_isolated
        return event

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recent_in_window(
        self,
        buf: deque[EnrichedEvent],
        anchor: EnrichedEvent,
        window: timedelta,
    ) -> list[EnrichedEvent]:
        """Return events in ``buf`` whose timestamps are within ``window``
        before (or at) ``anchor.timestamp``. Sorted chronologically.

        Using anchor.timestamp (event-time) rather than ctx.now() (walltime)
        keeps backtest and live behaviour identical: in a backtest, walltime
        compresses to seconds while event-time spans months, so a walltime
        cutoff would let the entire history into the window.
        """
        anchor_ts = anchor.print_.timestamp
        cutoff = anchor_ts - window
        return sorted(
            (e for e in buf if cutoff <= e.print_.timestamp <= anchor_ts),
            key=lambda e: e.print_.timestamp,
        )

    def _is_escalating(
        self,
        events_in_order: list[EnrichedEvent],
        ratio: float,
    ) -> bool:
        """True iff each successive premium >= ``ratio`` × previous.

        Returns False for fewer than 3 events.
        """
        if len(events_in_order) < 3:
            return False
        ratio_d = Decimal(str(ratio))
        for prev, cur in pairwise(events_in_order):
            if cur.print_.premium_paid < prev.print_.premium_paid * ratio_d:
                return False
        return True

    def _nearby_strike_count(
        self,
        buffers: dict[ClusterKey, deque[EnrichedEvent]],
        anchor: EnrichedEvent,
        nearby_n: int,
        window: timedelta,
    ) -> int:
        """Count recent events at strikes nearby ``anchor.strike`` (same
        ticker, expiry, option_type), within the window.

        "Nearby" = the ``nearby_n`` distinct strikes nearest to the anchor's
        strike (excluding the anchor's strike itself). This is robust to
        chain grid variations: a $1-grid AAPL chain and a $5-grid SPY chain
        both interpret nearby_n=1 as "the closest strike on either side".

        Anchor's same-strike events are NOT included — this method
        complements ``_recent_in_window`` for the same-strike check.
        """
        anchor_print = anchor.print_
        anchor_strike = anchor_print.strike

        # Find all buffer keys matching (ticker, expiry, option_type) with
        # different strikes. Sort by strike distance, take the ``nearby_n``
        # closest distinct strikes.
        candidate_keys = [
            k for k in buffers
            if k[0] == anchor_print.ticker
            and k[2] == anchor_print.expiry.isoformat()
            and k[3] == anchor_print.option_type
            and Decimal(k[1]) != anchor_strike
        ]
        # Sort by distance from anchor_strike (ascending).
        candidate_keys.sort(key=lambda k: abs(Decimal(k[1]) - anchor_strike))
        nearby_keys = candidate_keys[:nearby_n]

        # Count recent events across the nearby buffers.
        total = 0
        for k in nearby_keys:
            for e in buffers[k]:
                if (
                    anchor_print.timestamp - window
                    <= e.print_.timestamp
                    <= anchor_print.timestamp
                ):
                    total += 1
        return total
