"""Equivalence test: ``ClusterDecayStage`` vs ``ClusterDecayWatcher.decay_once``.

Phase 3.1.1 refactor changes the decay clock from walltime
(``ctx.now()``) to the incoming event's timestamp. This file pins
that for SAME-INPUT scenarios both produce SAME-OUTPUT, so the
refactor preserves observable behaviour wherever walltime ~ event-time
(i.e., live mode and the existing unit tests, where ``ctx.clock`` is
already injected to event-time-equivalent values).

The test does NOT prove the refactor produces identical results in
backtest replay — that's the point of the refactor: the new stage is
CORRECT in replay where the old watcher was BROKEN. So we only assert
equivalence in scenarios the old watcher could handle correctly.

Scenario coverage (mirrors ``test_cluster_decay.py`` decay_once cases):
  - Fresh buffer, no decay: both produce no flips.
  - Stale buffer, decay fires: both flip the same event.
  - Already-flagged buffer: both skip.
  - Two-buffer mix (fresh + stale): both flip only the stale one.
  - Empty buffer: both no-op.

Construction note: the new stage takes its clock from
``incoming_event.print_.timestamp``, the old watcher takes it from
``ctx.now()``. The test fixture sets ``ctx.clock`` to return the
incoming event's timestamp, so the two clocks agree.

THIS TEST IS REMOVED IN PHASE 3.1.2 along with the deprecated watcher
class. By that point the new stage is the only implementation and
there's nothing to compare against.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.cluster_decay import ClusterDecayWatcher
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
    """Build an EnrichedEvent for buffer fixtures."""
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


def _make_ctx(now: datetime) -> PipelineContext:
    """Context whose clock returns ``now`` — both implementations agree."""
    return PipelineContext(profile=load_default_profile(), clock=lambda: now)


def _flagged_set(buffers: dict) -> set[str]:  # type: ignore[type-arg]
    """Collect event_ids that are currently flagged across all buffers."""
    out: set[str] = set()
    for buf in buffers.values():
        for ev in buf:
            if ev.cluster_decayed:
                out.add(ev.print_.event_id)
    return out


def _run_old_watcher(ctx: PipelineContext) -> set[str]:
    """Apply old watcher to ctx; return flagged event_ids after."""
    ClusterDecayWatcher(ctx).decay_once()
    return _flagged_set(ctx.cluster_buffers)


async def _run_new_stage(
    ctx: PipelineContext, incoming: EnrichedEvent,
) -> set[str]:
    """Apply new stage to ctx with ``incoming`` as the clock-anchor event."""
    await ClusterDecayStage().enrich(incoming, ctx)
    return _flagged_set(ctx.cluster_buffers)


# ---------------------------------------------------------------------------
# Fresh buffer: neither flips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_fresh_buffer_no_flips() -> None:
    """5 min after the event with decay_minutes=90 → both produce no flips."""
    now = _BASE_TS + timedelta(minutes=5)
    incoming = _event(event_id="incoming", ts_offset_min=5)

    # Old watcher path
    ctx_old = _make_ctx(now)
    buffered = _event(event_id="buffered", ts_offset_min=0)
    ctx_old.cluster_buffers[cluster_key(buffered)] = deque([buffered])
    old_flagged = _run_old_watcher(ctx_old)

    # New stage path — fresh ctx, same buffer setup
    ctx_new = _make_ctx(now)
    buffered2 = _event(event_id="buffered", ts_offset_min=0)
    ctx_new.cluster_buffers[cluster_key(buffered2)] = deque([buffered2])
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == set()


# ---------------------------------------------------------------------------
# Stale buffer: both flip the same event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_stale_buffer_both_flip() -> None:
    """100 min after with decay_minutes=90 → both flip the buffered event."""
    now = _BASE_TS + timedelta(minutes=100)
    incoming = _event(event_id="incoming", ts_offset_min=100)

    # Old
    ctx_old = _make_ctx(now)
    buffered = _event(event_id="stale", ts_offset_min=0)
    ctx_old.cluster_buffers[cluster_key(buffered)] = deque([buffered])
    old_flagged = _run_old_watcher(ctx_old)

    # New
    ctx_new = _make_ctx(now)
    buffered2 = _event(event_id="stale", ts_offset_min=0)
    ctx_new.cluster_buffers[cluster_key(buffered2)] = deque([buffered2])
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == {"stale"}


# ---------------------------------------------------------------------------
# Already-flagged: both skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_already_flagged_both_preserve() -> None:
    """Pre-flagged event stays flagged in both paths; no double-counting."""
    now = _BASE_TS + timedelta(minutes=200)
    incoming = _event(event_id="incoming", ts_offset_min=200)

    ctx_old = _make_ctx(now)
    buffered = _event(event_id="already", ts_offset_min=0)
    buffered.cluster_decayed = True
    ctx_old.cluster_buffers[cluster_key(buffered)] = deque([buffered])
    old_flagged = _run_old_watcher(ctx_old)

    ctx_new = _make_ctx(now)
    buffered2 = _event(event_id="already", ts_offset_min=0)
    buffered2.cluster_decayed = True
    ctx_new.cluster_buffers[cluster_key(buffered2)] = deque([buffered2])
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == {"already"}


# ---------------------------------------------------------------------------
# Two buffers (one fresh, one stale): both flip only the stale one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_two_buffers_one_stale() -> None:
    now = _BASE_TS + timedelta(minutes=125)
    incoming = _event(event_id="incoming", strike="220", ts_offset_min=125)

    def _build_ctx() -> PipelineContext:
        ctx = _make_ctx(now)
        fresh = _event(event_id="fresh", strike="200", ts_offset_min=120)
        stale = _event(event_id="stale", strike="210", ts_offset_min=0)
        ctx.cluster_buffers[cluster_key(fresh)] = deque([fresh])
        ctx.cluster_buffers[cluster_key(stale)] = deque([stale])
        return ctx

    ctx_old = _build_ctx()
    old_flagged = _run_old_watcher(ctx_old)

    ctx_new = _build_ctx()
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == {"stale"}


# ---------------------------------------------------------------------------
# Empty buffer: both no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_empty_buffer_both_no_op() -> None:
    now = _BASE_TS + timedelta(minutes=200)
    incoming = _event(event_id="incoming", ts_offset_min=200)

    ctx_old = _make_ctx(now)
    ctx_old.cluster_buffers[("AAPL", "200", "2025-07-18", "call")] = deque()
    old_flagged = _run_old_watcher(ctx_old)

    ctx_new = _make_ctx(now)
    ctx_new.cluster_buffers[("AAPL", "200", "2025-07-18", "call")] = deque()
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == set()


# ---------------------------------------------------------------------------
# Boundary: exactly at decay threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_exact_threshold_both_flip() -> None:
    """Age == decay_minutes triggers decay in both (>= comparison)."""
    decay_min = load_default_profile().cluster.decay_minutes
    now = _BASE_TS + timedelta(minutes=decay_min)
    incoming = _event(event_id="incoming", ts_offset_min=decay_min)

    ctx_old = _make_ctx(now)
    buffered = _event(event_id="boundary", ts_offset_min=0)
    ctx_old.cluster_buffers[cluster_key(buffered)] = deque([buffered])
    old_flagged = _run_old_watcher(ctx_old)

    ctx_new = _make_ctx(now)
    buffered2 = _event(event_id="boundary", ts_offset_min=0)
    ctx_new.cluster_buffers[cluster_key(buffered2)] = deque([buffered2])
    new_flagged = await _run_new_stage(ctx_new, incoming)

    assert old_flagged == new_flagged == {"boundary"}
