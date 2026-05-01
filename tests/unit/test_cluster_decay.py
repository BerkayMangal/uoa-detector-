"""Tests for ``ClusterDecayWatcher`` — Module 38's decay companion.

Two layers:

  1. Unit tests on ``decay_once()``: deterministic single-pass scans with
     an injected clock so we can assert exactly what gets flagged.
  2. Integration test demonstrating the labeler downgrades a CONVEXITY_
     CLUSTER signal to CONVEXITY_WATCH on a re-label after the watcher
     sets ``cluster_decayed=True`` — the spec's stated reason for the
     watcher's existence.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.labeling.labeler import Labeler
from uoa_detector.pipeline.cluster_decay import ClusterDecayWatcher
from uoa_detector.pipeline.stage import PipelineContext, cluster_key

_BASE_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _event(
    *,
    event_id: str = "e1",
    ticker: str = "AAPL",
    strike: str = "200",
    ts_offset_min: float = 0.0,
) -> EnrichedEvent:
    return EnrichedEvent(
        print=OptionsPrint(
            event_id=event_id,
            timestamp=_BASE_TS + timedelta(minutes=ts_offset_min),
            ticker=ticker,
            option_type="call",
            strike=Decimal(strike),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal("1000"),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=False,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )


def _ctx_at_walltime(walltime: datetime) -> PipelineContext:
    """A PipelineContext whose injected clock returns a fixed walltime.

    Lets tests deterministically position 'now' relative to event timestamps.
    """
    return PipelineContext(profile=load_default_profile(), clock=lambda: walltime)


# ---------------------------------------------------------------------------
# decay_once: synchronous single-pass behavior
# ---------------------------------------------------------------------------


def test_fresh_buffer_is_not_decayed() -> None:
    """Most-recent event 5 min ago, decay_minutes=90 → not stale."""
    event = _event(ts_offset_min=0)
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=5))
    ctx.cluster_buffers[cluster_key(event)] = deque([event])

    flipped = ClusterDecayWatcher(ctx).decay_once()

    assert flipped == 0
    assert event.cluster_decayed is False


def test_stale_buffer_is_marked_decayed() -> None:
    """Most-recent event 100 min ago, decay_minutes=90 → stale → flagged."""
    event = _event(ts_offset_min=0)
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=100))
    ctx.cluster_buffers[cluster_key(event)] = deque([event])

    flipped = ClusterDecayWatcher(ctx).decay_once()

    assert flipped == 1
    assert event.cluster_decayed is True


def test_already_flagged_buffer_does_not_recount() -> None:
    """A buffer flagged on a prior pass is not 'newly decayed' on the next."""
    event = _event(ts_offset_min=0)
    event.cluster_decayed = True  # pre-set as if from a prior pass
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=200))
    ctx.cluster_buffers[cluster_key(event)] = deque([event])

    flipped = ClusterDecayWatcher(ctx).decay_once()

    assert flipped == 0
    # Still True — watcher didn't unset it either.
    assert event.cluster_decayed is True


def test_only_most_recent_event_in_buffer_is_flagged() -> None:
    """A buffer with multiple events: only the last one gets the flag."""
    e1 = _event(event_id="old", ts_offset_min=0)
    e2 = _event(event_id="newer", ts_offset_min=10)
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=200))
    ctx.cluster_buffers[cluster_key(e1)] = deque([e1, e2])

    ClusterDecayWatcher(ctx).decay_once()

    assert e1.cluster_decayed is False
    assert e2.cluster_decayed is True


def test_empty_buffer_is_skipped() -> None:
    """A buffer with no events doesn't crash the watcher."""
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=200))
    ctx.cluster_buffers[("AAPL", "200", "2025-07-18", "call")] = deque()

    flipped = ClusterDecayWatcher(ctx).decay_once()
    assert flipped == 0


def test_decay_minutes_boundary_exactly_at_threshold_is_decayed() -> None:
    """Age == decay_minutes (>= comparison) → decayed."""
    event = _event(ts_offset_min=0)
    decay_min = load_default_profile().cluster.decay_minutes
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=decay_min))
    ctx.cluster_buffers[cluster_key(event)] = deque([event])

    flipped = ClusterDecayWatcher(ctx).decay_once()

    assert flipped == 1
    assert event.cluster_decayed is True


def test_two_buffers_one_stale_one_fresh_flips_only_the_stale_one() -> None:
    """Watcher handles each buffer independently."""
    fresh = _event(event_id="fresh", strike="200", ts_offset_min=120)
    stale = _event(event_id="stale", strike="210", ts_offset_min=0)
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=125))
    ctx.cluster_buffers[cluster_key(fresh)] = deque([fresh])
    ctx.cluster_buffers[cluster_key(stale)] = deque([stale])

    flipped = ClusterDecayWatcher(ctx).decay_once()

    assert flipped == 1
    assert fresh.cluster_decayed is False
    assert stale.cluster_decayed is True


# ---------------------------------------------------------------------------
# Integration: labeler downgrades CONVEXITY_CLUSTER → CONVEXITY_WATCH
# after the watcher fires
# ---------------------------------------------------------------------------


def test_labeler_downgrades_after_watcher_fires() -> None:
    """The acceptance criterion from the Phase 2 prompt: a CONVEXITY_CLUSTER
    signal becomes CONVEXITY_WATCH when re-labeled after the decay watcher
    has flipped ``cluster_decayed=True``.
    """
    profile = load_default_profile()
    labeler = Labeler(profile)

    # Build an event with sub-scores that produce CONVEXITY_CLUSTER:
    # cluster_density_score >= cluster_min, but neither convexity nor uoa
    # is high enough to trigger CONVEXITY_BURST or upgrade to HCS.
    event = _event(ts_offset_min=0)
    event.cluster_density_score = profile.label_thresholds.cluster_min
    event.convexity_score = 0.5
    event.uoa_score = 0.4
    event.event_score = 0.0
    event.gamma_score = 0.0
    event.price_confirmation_score = 0.0
    event.sector_confirmation_score = 0.0
    event.time_of_day_weight = 0.7
    event.relative_premium_score = 0.0
    event.dte_multiplier_applied = 1.0
    event.combined_score_pre_penalty = 0.5
    event.combined_score_post_penalty = 0.5

    # Pre-watcher: labeler returns CONVEXITY_CLUSTER.
    decision_before = labeler.decide(event)
    assert decision_before.label == SignalLabel.CONVEXITY_CLUSTER

    # Place this event into a cluster buffer that will go stale.
    ctx = _ctx_at_walltime(_BASE_TS + timedelta(minutes=200))
    ctx.cluster_buffers[cluster_key(event)] = deque([event])

    # Run the watcher — should flip cluster_decayed.
    flipped = ClusterDecayWatcher(ctx).decay_once()
    assert flipped == 1
    assert event.cluster_decayed is True

    # Post-watcher: labeler returns CONVEXITY_WATCH (downgraded).
    decision_after = labeler.decide(event)
    assert decision_after.label == SignalLabel.CONVEXITY_WATCH
    assert "decayed" in decision_after.reason.lower()


# ---------------------------------------------------------------------------
# Async run() loop: cancellation is responsive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_exits_on_stop() -> None:
    """``stop()`` causes ``run()`` to exit at its next wakeup."""
    import asyncio

    ctx = _ctx_at_walltime(_BASE_TS)
    watcher = ClusterDecayWatcher(ctx, check_interval_s=0.05)

    task = asyncio.create_task(watcher.run())
    # Let the loop start.
    await asyncio.sleep(0.02)
    watcher.stop()
    # Should exit within one check_interval_s + some scheduling slack.
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert task.exception() is None
