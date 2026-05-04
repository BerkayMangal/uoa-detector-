"""Unit tests for ``ClusterDecayStage`` — the event-time decay check.

Phase 3.1.1 introduced this stage as the replacement for the walltime
``ClusterDecayWatcher`` asyncio task. These tests pin the stage's
behaviour DIRECTLY (no comparison against the legacy watcher) so they
survive Phase 3.1.2's watcher removal.

Conventions:
  - Each test constructs an incoming event with a specific timestamp;
    that timestamp serves as the decay-clock anchor.
  - Older buffer events are positioned relative to the incoming
    timestamp, so we can predict exactly which ones cross the
    decay_minutes boundary.
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
from uoa_detector.pipeline.stage import PipelineContext, cluster_key
from uoa_detector.pipeline.stages.cluster_decay_stage import ClusterDecayStage

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


def _ctx() -> PipelineContext:
    """Default-profile context. Stage uses event timestamps, not ctx.clock."""
    return PipelineContext(profile=load_default_profile())


# ---------------------------------------------------------------------------
# Direct stage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_buffer_no_decay_under_window() -> None:
    """Buffered event 5 min before incoming, decay_minutes=90 → no flip."""
    incoming = _event(event_id="incoming", ts_offset_min=5)
    buffered = _event(event_id="buffered", ts_offset_min=0)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(buffered)] = deque([buffered])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert buffered.cluster_decayed is False


@pytest.mark.asyncio
async def test_stale_buffer_decays() -> None:
    """Buffered event 100 min before incoming, decay_minutes=90 → flip."""
    incoming = _event(event_id="incoming", ts_offset_min=100)
    stale = _event(event_id="stale", ts_offset_min=0)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(stale)] = deque([stale])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert stale.cluster_decayed is True


@pytest.mark.asyncio
async def test_already_flagged_buffer_unchanged() -> None:
    incoming = _event(event_id="incoming", ts_offset_min=200)
    pre_flagged = _event(event_id="pre", ts_offset_min=0)
    pre_flagged.cluster_decayed = True

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(pre_flagged)] = deque([pre_flagged])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert pre_flagged.cluster_decayed is True  # still flagged


@pytest.mark.asyncio
async def test_only_most_recent_event_in_buffer_flagged() -> None:
    """Buffer with multiple events: only the last one gets the flag."""
    incoming = _event(event_id="incoming", ts_offset_min=200)
    older = _event(event_id="older", ts_offset_min=0)
    newer = _event(event_id="newer", ts_offset_min=10)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(older)] = deque([older, newer])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert older.cluster_decayed is False  # not the most-recent
    assert newer.cluster_decayed is True   # most-recent, age 190 min > 90


@pytest.mark.asyncio
async def test_empty_buffer_does_not_crash() -> None:
    incoming = _event(event_id="incoming", ts_offset_min=200)

    ctx = _ctx()
    ctx.cluster_buffers[("AAPL", "200", "2025-07-18", "call")] = deque()

    await ClusterDecayStage().enrich(incoming, ctx)  # no exception


@pytest.mark.asyncio
async def test_exact_decay_threshold_flips_inclusive() -> None:
    """Age == decay_minutes triggers decay (>= comparison)."""
    decay_min = load_default_profile().cluster.decay_minutes
    incoming = _event(event_id="incoming", ts_offset_min=decay_min)
    buffered = _event(event_id="boundary", ts_offset_min=0)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(buffered)] = deque([buffered])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert buffered.cluster_decayed is True


@pytest.mark.asyncio
async def test_two_buffers_independent() -> None:
    """Each buffer evaluated separately."""
    incoming = _event(event_id="incoming", ts_offset_min=125)
    fresh = _event(event_id="fresh", strike="200", ts_offset_min=120)
    stale = _event(event_id="stale", strike="210", ts_offset_min=0)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(fresh)] = deque([fresh])
    ctx.cluster_buffers[cluster_key(stale)] = deque([stale])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert fresh.cluster_decayed is False
    assert stale.cluster_decayed is True


@pytest.mark.asyncio
async def test_incoming_event_never_flagged_on_own_pass() -> None:
    """The incoming event is in its own buffer (M38 ran before us); the stage
    should NOT flag it because age = 0 (event timestamp == its own).
    """
    incoming = _event(event_id="self", ts_offset_min=200)

    ctx = _ctx()
    # Simulate M38 having appended the incoming event already.
    ctx.cluster_buffers[cluster_key(incoming)] = deque([incoming])

    await ClusterDecayStage().enrich(incoming, ctx)

    assert incoming.cluster_decayed is False


# ---------------------------------------------------------------------------
# Integration: labeler downgrades CONVEXITY_CLUSTER → CONVEXITY_WATCH
# after the stage fires (Phase 2 acceptance criterion #9, preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_labeler_downgrades_after_stage_fires() -> None:
    """Equivalent to the Phase 2 watcher integration test, retargeted at the
    new stage. Decayed CONVEXITY_CLUSTER → CONVEXITY_WATCH downgrade.
    """
    profile = load_default_profile()
    labeler = Labeler(profile)

    # Buffered event with sub-scores that label CONVEXITY_CLUSTER.
    buffered = _event(event_id="cluster", ts_offset_min=0)
    buffered.cluster_density_score = profile.label_thresholds.cluster_min
    buffered.convexity_score = 0.5
    buffered.uoa_score = 0.4
    buffered.event_score = 0.0
    buffered.gamma_score = 0.0
    buffered.price_confirmation_score = 0.0
    buffered.sector_confirmation_score = 0.0
    buffered.time_of_day_weight = 0.7
    buffered.relative_premium_score = 0.0
    buffered.dte_multiplier_applied = 1.0
    buffered.combined_score_pre_penalty = 0.5
    buffered.combined_score_post_penalty = 0.5

    # Pre-stage: labeler returns CONVEXITY_CLUSTER.
    decision_before = labeler.decide(buffered)
    assert decision_before.label == SignalLabel.CONVEXITY_CLUSTER

    # Incoming event 200 min later — well past 90-min decay window.
    incoming = _event(event_id="trigger", ts_offset_min=200)

    ctx = _ctx()
    ctx.cluster_buffers[cluster_key(buffered)] = deque([buffered])

    await ClusterDecayStage().enrich(incoming, ctx)
    assert buffered.cluster_decayed is True

    # Post-stage: labeler returns CONVEXITY_WATCH (downgraded).
    decision_after = labeler.decide(buffered)
    assert decision_after.label == SignalLabel.CONVEXITY_WATCH
    assert "decayed" in decision_after.reason.lower()
