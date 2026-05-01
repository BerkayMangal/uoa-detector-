"""Unit tests for ``SweepBlockStage`` (Module 34).

Coverage:
  - Classification: iso / sweep / block / sweep+above_ask, including the
    timestamp-skew window check.
  - Bonus adjustments emitted as ``ScoreAdjustment`` with the right delta
    matching the classification (NOT mutating uoa_score directly).
  - Non-stacking: a print classified as ISO does NOT also get the cross-
    venue bonus, even if it happens to span multiple exchanges.
  - Idempotency: running the stage twice does not double-add the adjustment.
  - Pre-set sweep_classification is honored (override-stage pattern).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m34_sweep_block import SweepBlockStage


def _print(
    *,
    is_iso: bool = False,
    fill_side: str = "above_ask",
    exchanges: tuple[str, ...] = ("CBOE",),
    timestamp_skew_ms: int = 0,
) -> OptionsPrint:
    """Build an OptionsPrint with explicit source_agreement for sweep tests."""
    sa = SourceAgreement(
        sources_seen=("a",) if len(exchanges) == 1 else ("a", "b"),
        premium_disagreement=Decimal("0"),
        timestamp_skew_ms=timestamp_skew_ms,
        classification_disagreement=False,
        confidence_tier="single" if len(exchanges) == 1 else "unanimous",
        exchanges_seen=exchanges,
    )
    return OptionsPrint(
        event_id="t-1",
        timestamp=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("100"),
        option_price=Decimal("1.50"),
        implied_volatility=0.45,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side=fill_side,  # type: ignore[arg-type]
        exchange=exchanges[0] if exchanges else "",
        is_iso=is_iso,
        open_interest=1500,
        source_agreement=sa,
    )


def _ctx() -> PipelineContext:
    return PipelineContext(profile=load_default_profile())


def _adjustments_to_uoa(event: EnrichedEvent) -> list:
    return [a for a in event.score_adjustments if a.target == "uoa_score"]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iso_print_classified_iso_with_iso_bonus() -> None:
    event = EnrichedEvent(print=_print(is_iso=True))
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "iso"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
    assert adjs[0].delta == ctx.profile.sweep.iso_bonus
    assert adjs[0].source_module == "m34_sweep_block"


@pytest.mark.asyncio
async def test_multi_venue_above_ask_classified_sweep_with_above_ask_bonus() -> None:
    event = EnrichedEvent(
        print=_print(
            is_iso=False,
            fill_side="above_ask",
            exchanges=("CBOE", "ISE"),
            timestamp_skew_ms=20,  # within 50ms window
        ),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "sweep"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
    assert adjs[0].delta == ctx.profile.sweep.cross_venue_above_ask_bonus


@pytest.mark.asyncio
async def test_multi_venue_at_ask_classified_sweep_with_plain_bonus() -> None:
    event = EnrichedEvent(
        print=_print(
            is_iso=False,
            fill_side="at_ask",
            exchanges=("CBOE", "ISE"),
            timestamp_skew_ms=20,
        ),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "sweep"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
    assert adjs[0].delta == ctx.profile.sweep.cross_venue_bonus


@pytest.mark.asyncio
async def test_single_venue_classified_block_with_no_bonus() -> None:
    event = EnrichedEvent(
        print=_print(is_iso=False, exchanges=("CBOE",)),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "block"
    assert _adjustments_to_uoa(out) == []
    # uoa_score baseline still set
    assert out.uoa_score == 0.5


@pytest.mark.asyncio
async def test_multi_venue_outside_window_is_block_not_sweep() -> None:
    """Two venues but timestamp_skew_ms exceeds cross_venue_window_ms → block."""
    event = EnrichedEvent(
        print=_print(
            is_iso=False,
            exchanges=("CBOE", "ISE"),
            timestamp_skew_ms=100,  # > default 50ms cross_venue_window_ms
        ),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)
    assert out.sweep_classification == "block"
    assert _adjustments_to_uoa(out) == []


# ---------------------------------------------------------------------------
# Non-stacking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iso_takes_precedence_over_sweep_no_double_bonus() -> None:
    """A print that is BOTH ISO AND multi-venue gets the ISO bonus only,
    not ISO+sweep stacked.
    """
    event = EnrichedEvent(
        print=_print(
            is_iso=True,
            fill_side="above_ask",
            exchanges=("CBOE", "ISE"),
            timestamp_skew_ms=20,
        ),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "iso"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1  # NOT 2 (no stacking)
    assert adjs[0].delta == ctx.profile.sweep.iso_bonus


@pytest.mark.asyncio
async def test_sweep_above_ask_does_not_stack_plain_sweep() -> None:
    """The above-ask sweep bonus is the only one emitted, not above-ask + plain."""
    event = EnrichedEvent(
        print=_print(
            is_iso=False,
            fill_side="above_ask",
            exchanges=("CBOE", "ISE", "PHLX"),  # 3 venues, all within window
            timestamp_skew_ms=10,
        ),
    )
    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "sweep"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
    assert adjs[0].delta == ctx.profile.sweep.cross_venue_above_ask_bonus


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_stage_twice_does_not_double_add_adjustment() -> None:
    event = EnrichedEvent(print=_print(is_iso=True))
    ctx = _ctx()
    stage = SweepBlockStage()
    await stage.enrich(event, ctx)
    await stage.enrich(event, ctx)

    adjs = _adjustments_to_uoa(event)
    assert len(adjs) == 1


# ---------------------------------------------------------------------------
# Pre-set classification (override stage pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_set_classification_is_honored_and_matching_bonus_emitted() -> None:
    """If the override stage pinned 'sweep' but the print is single-venue,
    M34 honors the pinned classification and still emits the cross-venue
    bonus matching it.
    """
    event = EnrichedEvent(print=_print(is_iso=False, exchanges=("CBOE",)))
    event.sweep_classification = "sweep"  # pinned by override
    # _print's default fill_side is "above_ask" → above-ask bonus expected.

    ctx = _ctx()
    out = await SweepBlockStage().enrich(event, ctx)

    assert out.sweep_classification == "sweep"
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
    # fill_side defaults to above_ask in _print → above-ask bonus
    assert adjs[0].delta == ctx.profile.sweep.cross_venue_above_ask_bonus


@pytest.mark.asyncio
async def test_pre_set_block_classification_emits_no_bonus() -> None:
    event = EnrichedEvent(
        print=_print(is_iso=True),  # would normally classify ISO
    )
    event.sweep_classification = "block"  # pinned by override

    out = await SweepBlockStage().enrich(event, _ctx())

    assert out.sweep_classification == "block"
    assert _adjustments_to_uoa(out) == []


# ---------------------------------------------------------------------------
# uoa_score baseline behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uoa_score_baseline_set_when_none() -> None:
    """If uoa_score is None on entry, M34 sets it to 0.5 baseline."""
    event = EnrichedEvent(print=_print(is_iso=False, exchanges=("CBOE",)))
    assert event.uoa_score is None
    out = await SweepBlockStage().enrich(event, _ctx())
    assert out.uoa_score == 0.5


@pytest.mark.asyncio
async def test_pre_set_uoa_score_is_not_overwritten() -> None:
    """Override-stage may pin uoa_score; M34 still emits the bonus but
    leaves the underlying score alone.
    """
    event = EnrichedEvent(print=_print(is_iso=True))
    event.uoa_score = 0.85
    out = await SweepBlockStage().enrich(event, _ctx())
    assert out.uoa_score == 0.85  # unchanged
    # Bonus still emitted as adjustment.
    adjs = _adjustments_to_uoa(out)
    assert len(adjs) == 1
