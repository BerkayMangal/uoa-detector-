"""``score_breakdown`` must add up to the score it claims to explain.

Its docstring has always said the components sum to ``combined_score_pre_penalty``.
Nothing checked that. The 2026-09-19 audit found the sum was wrong for every event
carrying a cross-module adjustment aimed at the combined score — Phase 3.4.4's
rail, whose live user is M24's post-earnings IV spike penalty:
``compute_combined_score`` adds those adjustments to ``pre`` and ``score_breakdown``
did not carry them at all.

A breakdown that cannot reconstruct its own total is worse than none: it is shown
in the decision record, in the digest line and in the CSV export as the explanation
of a number it silently disagrees with.

Expected values here are computed from the profile's own weights, never by calling
``score_breakdown`` twice and comparing it with itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import ScoreAdjustment, single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.scoring.combined import compute_combined_score, score_breakdown

_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)

_COMPONENTS = (
    "uoa", "convexity", "event", "gamma",
    "price_confirmation", "sector_confirmation", "time_of_day",
)


def _event() -> EnrichedEvent:
    """An event with every sub-score populated, so no missing-score policy runs."""
    event = EnrichedEvent(
        print=OptionsPrint(
            event_id="evt-breakdown",
            timestamp=_TS,
            ticker="AAPL",
            option_type="call",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal("4500"),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=True,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )
    event.uoa_score = 0.85
    event.convexity_score = 0.6
    event.event_score = 0.5
    event.gamma_score = 0.4
    event.price_confirmation_score = 0.7
    event.sector_confirmation_score = 0.3
    event.time_of_day_weight = 1.0
    event.cluster_density_score = 0.8
    event.relative_premium_score = 1.0
    event.dte_multiplier_applied = 1.0
    return event


def _component_sum(breakdown: dict[str, float]) -> float:
    return sum(v for k, v in breakdown.items() if not k.startswith("penalty_"))


def test_the_breakdown_sums_to_the_score_it_explains() -> None:
    event = _event()
    pre, _post = compute_combined_score(event)
    assert _component_sum(score_breakdown(event)) == pytest.approx(pre)


def test_each_component_is_its_own_weight_times_its_own_sub_score() -> None:
    """Computed from the profile's weights, not from a second call into the code."""
    event = _event()
    weights = load_default_profile().scoring
    breakdown = score_breakdown(event)
    assert breakdown["uoa"] == pytest.approx(weights.uoa * 0.85)
    assert breakdown["convexity"] == pytest.approx(weights.convexity * 0.6)
    assert breakdown["event"] == pytest.approx(weights.event * 0.5)
    assert breakdown["gamma"] == pytest.approx(weights.gamma * 0.4)
    assert breakdown["price_confirmation"] == pytest.approx(
        weights.price_confirmation * 0.7,
    )
    assert breakdown["sector_confirmation"] == pytest.approx(
        weights.sector_confirmation * 0.3,
    )
    assert breakdown["time_of_day"] == pytest.approx(weights.time_of_day * 1.0)


def test_an_ordinary_event_carries_exactly_the_seven_component_keys() -> None:
    """No adjustment, no extra key — the shape consumers already render."""
    assert set(score_breakdown(_event())) == set(_COMPONENTS)


@pytest.mark.parametrize("target", ["combined_score_pre", "combined_score_pre_penalty"])
def test_a_combined_level_adjustment_is_inside_the_breakdown(target: str) -> None:
    """M24's rail. Both spellings reach ``pre``, so both must reach the breakdown.

    This is the assertion the audit's finding turns on: before the fix the
    breakdown omitted the adjustment entirely, so its components summed to
    ``pre - delta`` while the record displayed them as the explanation of ``pre``.
    """
    event = _event()
    event.score_adjustments.append(ScoreAdjustment(
        target=target,
        delta=-0.4,
        reason="M24 post-earnings IV spike",
        source_module="m24_iv_exhaustion",
    ))
    pre, _post = compute_combined_score(event)
    breakdown = score_breakdown(event)

    assert breakdown["adjustment_combined"] == pytest.approx(-0.4)
    assert _component_sum(breakdown) == pytest.approx(pre)
    # And the adjustment actually moved the score, or the assertions above are
    # satisfied by an event that never exercised the rail.
    unadjusted = _event()
    unadjusted_pre, _ = compute_combined_score(unadjusted)
    assert pre == pytest.approx(unadjusted_pre - 0.4)


def test_both_spellings_of_the_combined_target_accumulate() -> None:
    """``compute_combined_score`` adds both; a breakdown carrying one would still
    fail to reconstruct the total."""
    event = _event()
    for target, delta in (("combined_score_pre", -0.4), ("combined_score_pre_penalty", 0.1)):
        event.score_adjustments.append(ScoreAdjustment(
            target=target, delta=delta, reason="test", source_module="test",
        ))
    pre, _post = compute_combined_score(event)
    breakdown = score_breakdown(event)
    assert breakdown["adjustment_combined"] == pytest.approx(-0.3)
    assert _component_sum(breakdown) == pytest.approx(pre)


def test_a_sub_score_adjustment_stays_in_its_own_component() -> None:
    """The uoa rail was already handled; this pins that the fix did not double-count."""
    event = _event()
    event.score_adjustments.append(ScoreAdjustment(
        target="uoa_score", delta=0.15, reason="M34 sweep", source_module="m34_sweep_block",
    ))
    pre, _post = compute_combined_score(event)
    breakdown = score_breakdown(event)
    weights = load_default_profile().scoring
    assert breakdown["uoa"] == pytest.approx(weights.uoa * (0.85 + 0.15))
    assert "adjustment_combined" not in breakdown
    assert _component_sum(breakdown) == pytest.approx(pre)


def test_penalties_are_reported_apart_from_the_components() -> None:
    """``post`` is ``pre`` plus the penalties; the breakdown keeps the two separable."""
    event = _event()
    pre, post = compute_combined_score(event)
    breakdown = score_breakdown(event)
    penalties = sum(v for k, v in breakdown.items() if k.startswith("penalty_"))
    assert penalties == pytest.approx(post - pre)
