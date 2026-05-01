"""Unit tests for ``TemporalClusterStage`` (Module 38).

Coverage:
  - All five density bands: isolated (1), two_same (2), three_same (3+),
    three_escalating (3+ with size escalation), nearby_strikes (3+ at
    nearby strikes when same-strike count is below 2).
  - Escalation detection respects ``profile.cluster.escalation_ratio`` —
    a chain where a single step under-ratios the prior breaks escalation.
  - Window correctness: events outside ``window_minutes`` don't count.
  - Buffer is updated even when score is pre-set (idempotent on score,
    not on buffer state).
  - Idempotent re-run: same event twice doesn't double-count.
  - Pre-set ``cluster_density_score`` is honored.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext, cluster_key
from uoa_detector.pipeline.stages.m38_temporal_cluster import TemporalClusterStage

_BASE_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _event(
    *,
    event_id: str = "e1",
    ticker: str = "AAPL",
    strike: str = "200",
    expiry: date = date(2025, 7, 18),
    option_type: str = "call",
    ts_offset_min: float = 0.0,
    premium: str = "1000",
) -> EnrichedEvent:
    return EnrichedEvent(
        print=OptionsPrint(
            event_id=event_id,
            timestamp=_BASE_TS + timedelta(minutes=ts_offset_min),
            ticker=ticker,
            option_type=option_type,  # type: ignore[arg-type]
            strike=Decimal(strike),
            expiry=expiry,
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal(premium),
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
    return PipelineContext(profile=load_default_profile())


# ---------------------------------------------------------------------------
# Density bands (single anchor — buffer empty before)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolated_print_yields_density_isolated() -> None:
    event = _event()
    ctx = _ctx()
    out = await TemporalClusterStage().enrich(event, ctx)
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated
    assert out.cluster_density_score == 0.0
    assert out.escalating_cluster is False


@pytest.mark.asyncio
async def test_two_same_strike_yields_density_two_same() -> None:
    ctx = _ctx()
    stage = TemporalClusterStage()

    e1 = _event(event_id="e1", ts_offset_min=0)
    e2 = _event(event_id="e2", ts_offset_min=10)

    await stage.enrich(e1, ctx)
    out = await stage.enrich(e2, ctx)

    assert out.cluster_density_score == ctx.profile.cluster.density_two_same
    assert out.cluster_density_score == 0.4


@pytest.mark.asyncio
async def test_three_same_strike_no_escalation_yields_density_three_same() -> None:
    """All three premiums equal → not escalating → 0.8 band."""
    ctx = _ctx()
    stage = TemporalClusterStage()

    for i in range(3):
        ev = _event(event_id=f"e{i}", ts_offset_min=i * 10, premium="1000")
        out = await stage.enrich(ev, ctx)

    assert out.cluster_density_score == ctx.profile.cluster.density_three_same
    assert out.cluster_density_score == 0.8
    assert out.escalating_cluster is False


@pytest.mark.asyncio
async def test_three_same_strike_with_escalation_yields_density_three_escalating() -> None:
    """Each premium ≥ 1.20× previous → escalating → 1.0 band."""
    ctx = _ctx()
    stage = TemporalClusterStage()

    # 1000 → 1500 → 2500 (each ≥ 1.20× previous)
    premiums = ["1000", "1500", "2500"]
    out = None
    for i, p in enumerate(premiums):
        ev = _event(event_id=f"e{i}", ts_offset_min=i * 10, premium=p)
        out = await stage.enrich(ev, ctx)

    assert out is not None
    assert out.cluster_density_score == ctx.profile.cluster.density_three_escalating
    assert out.cluster_density_score == 1.0
    assert out.escalating_cluster is True


@pytest.mark.asyncio
async def test_escalation_breaks_when_one_step_under_ratio() -> None:
    """1000 → 1500 → 1700 — last step is only 1.13×, escalation broken."""
    ctx = _ctx()
    stage = TemporalClusterStage()

    premiums = ["1000", "1500", "1700"]
    out = None
    for i, p in enumerate(premiums):
        ev = _event(event_id=f"e{i}", ts_offset_min=i * 10, premium=p)
        out = await stage.enrich(ev, ctx)

    assert out is not None
    # 3 same-strike → density_three_same (NOT escalating)
    assert out.cluster_density_score == ctx.profile.cluster.density_three_same
    assert out.escalating_cluster is False


# ---------------------------------------------------------------------------
# Nearby-strike band
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nearby_strikes_band_when_same_strike_low() -> None:
    """One print at strike 200; three prints at strike 195 (the only 'nearby'
    strike). The strike-200 anchor should pick up density_nearby_strikes (0.6).

    Note: the stage's nearby check only fires when the SAME-strike count is
    < 2 (i.e., the higher-priority same-strike bands haven't matched). So
    we set up the strike-195 buffer first, then drop in the strike-200 anchor.
    """
    ctx = _ctx()
    stage = TemporalClusterStage()

    # 3 prints at strike 195 to make it qualify as a "nearby" cluster.
    for i in range(3):
        ev = _event(
            event_id=f"near-{i}",
            strike="195",
            ts_offset_min=i * 5,
        )
        await stage.enrich(ev, ctx)

    # Anchor at strike 200, shortly after.
    anchor = _event(event_id="anchor", strike="200", ts_offset_min=20)
    out = await stage.enrich(anchor, ctx)

    assert out.cluster_density_score == ctx.profile.cluster.density_nearby_strikes
    assert out.cluster_density_score == 0.6


@pytest.mark.asyncio
async def test_far_strikes_do_not_count_as_nearby() -> None:
    """Strike 195 is the closest to 200, but if there are also prints at 250,
    the 250 strikes are 'far' and don't count toward the 200's nearby band
    (only nearby_strike_count=1 strikes are scanned).

    With 1 print at strike 195 (not enough on its own) and 5 prints at
    strike 250 (far), the strike-200 anchor falls back to isolated.
    """
    ctx = _ctx()
    stage = TemporalClusterStage()

    # 1 print at strike 195 — not enough for nearby_strikes band on its own.
    await stage.enrich(_event(event_id="n1", strike="195", ts_offset_min=0), ctx)
    # 5 prints at strike 250 — far away, shouldn't count.
    for i in range(5):
        await stage.enrich(
            _event(event_id=f"far-{i}", strike="250", ts_offset_min=i + 1),
            ctx,
        )

    anchor = _event(event_id="anchor", strike="200", ts_offset_min=10)
    out = await stage.enrich(anchor, ctx)
    # 1 nearby + 0 far counted → 1 < 3 → isolated.
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated


# ---------------------------------------------------------------------------
# Window correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_events_outside_window_do_not_count() -> None:
    """An event 120 minutes old is outside the default 60-minute window,
    so a current event should not see it as a cluster mate.
    """
    ctx = _ctx()
    stage = TemporalClusterStage()

    # Old event 120 minutes ago.
    old = _event(event_id="old", ts_offset_min=0)
    await stage.enrich(old, ctx)

    # Current event 120 minutes later — same strike but outside window.
    current = _event(event_id="cur", ts_offset_min=120)
    out = await stage.enrich(current, ctx)

    # Only current event in the 60-minute window → isolated.
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_set_cluster_density_score_is_not_overwritten() -> None:
    event = _event()
    event.cluster_density_score = 0.95  # pre-set by override stage
    ctx = _ctx()
    out = await TemporalClusterStage().enrich(event, ctx)
    assert out.cluster_density_score == 0.95


@pytest.mark.asyncio
async def test_buffer_still_updated_when_score_pre_set() -> None:
    """Buffer accumulates even when scoring is skipped — downstream events
    in the same scenario must see this print.
    """
    ctx = _ctx()
    stage = TemporalClusterStage()

    e1 = _event(event_id="e1", ts_offset_min=0)
    e1.cluster_density_score = 0.95  # pre-set
    await stage.enrich(e1, ctx)

    # Buffer should contain e1 even though score was pre-set.
    key = cluster_key(e1)
    buf = ctx.cluster_buffers[key]
    assert len(buf) == 1
    assert buf[0].print_.event_id == "e1"


@pytest.mark.asyncio
async def test_running_stage_twice_does_not_double_count() -> None:
    """Same event twice: buffer length stays at 1, density unchanged."""
    ctx = _ctx()
    stage = TemporalClusterStage()
    event = _event(event_id="e1")
    await stage.enrich(event, ctx)
    out = await stage.enrich(event, ctx)

    key = cluster_key(event)
    assert len(ctx.cluster_buffers[key]) == 1
    # Single print → still isolated.
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated


# ---------------------------------------------------------------------------
# Bucket key separation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_tickers_have_separate_buffers() -> None:
    """Two prints same strike/expiry but different tickers → not clustered."""
    ctx = _ctx()
    stage = TemporalClusterStage()

    a = _event(event_id="a", ticker="AAPL")
    b = _event(event_id="b", ticker="MSFT")

    await stage.enrich(a, ctx)
    out = await stage.enrich(b, ctx)

    # MSFT alone in its bucket → isolated.
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated


@pytest.mark.asyncio
async def test_calls_and_puts_have_separate_buffers() -> None:
    """Same ticker/strike/expiry, different option_type → distinct buffers."""
    ctx = _ctx()
    stage = TemporalClusterStage()

    c = _event(event_id="c", option_type="call")
    p = _event(event_id="p", option_type="put")

    await stage.enrich(c, ctx)
    out = await stage.enrich(p, ctx)
    assert out.cluster_density_score == ctx.profile.cluster.density_isolated
