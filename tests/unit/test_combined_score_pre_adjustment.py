"""Phase 3.4.4.2 tests for combined_score_pre ScoreAdjustment rail.

Pins:
  - Adjustments targeting 'combined_score_pre' are summed into pre
  - Adjustments targeting 'combined_score_pre_penalty' (alias)
    are also summed into pre
  - Multiple adjustments targeting the same field accumulate
  - Adjustments targeting unrelated fields don't affect pre
  - Existing 'uoa_score' adjustments still work (back-compat)
  - The penalty engine (applied_penalties) still applies
    additionally on top of pre adjustments
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import ScoreAdjustment, SourceAgreement
from uoa_detector.domain.events import (
    AppliedPenalty,
    EnrichedEvent,
    OptionsPrint,
)
from uoa_detector.scoring.combined import compute_combined_score


def _make_fully_scored_event() -> EnrichedEvent:
    """Build an event with all sub-scores set + dte_multiplier."""
    op = OptionsPrint(
        event_id="e1",
        timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=Decimal("150.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.25,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("synthetic",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    e = EnrichedEvent(print=op)
    e.uoa_score = 0.5
    e.convexity_score = 0.5
    e.event_score = 0.5
    e.gamma_score = 0.5
    e.price_confirmation_score = 0.5
    e.sector_confirmation_score = 0.5
    e.time_of_day_weight = 0.5
    e.dte_multiplier_applied = 1.0
    return e


def test_baseline_score_no_adjustments() -> None:
    """All sub-scores=0.5, multiplier=1.0 → pre = 0.5 (sum of weighted)."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    pre, post = compute_combined_score(event, profile)
    # All weights sum to 1.0 → 0.5 * 1.0 = 0.5
    assert pre == 0.5
    assert post == 0.5  # no penalties applied


def test_adjustment_targeting_combined_score_pre_subtracts() -> None:
    """ScoreAdjustment(target='combined_score_pre', delta=-0.4) → pre - 0.4."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.4,
        reason="M24 post-earnings IV spike",
        source_module="m24_iv_exhaustion",
    ))
    pre, _post = compute_combined_score(event, profile)
    assert pre == 0.5 + (-0.4)
    assert pre == 0.10000000000000003 or abs(pre - 0.1) < 1e-9


def test_adjustment_targeting_combined_score_pre_penalty_alias() -> None:
    """Alias target 'combined_score_pre_penalty' (actual field name) works."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre_penalty",
        delta=-0.2,
        reason="alias test",
        source_module="test",
    ))
    pre, _ = compute_combined_score(event, profile)
    assert abs(pre - 0.3) < 1e-9


def test_multiple_adjustments_same_target_accumulate() -> None:
    """Two -0.2 adjustments → -0.4 total."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.2,
        reason="first",
        source_module="m24_iv_exhaustion",
    ))
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.2,
        reason="second",
        source_module="future_module",
    ))
    pre, _ = compute_combined_score(event, profile)
    assert abs(pre - 0.1) < 1e-9


def test_adjustment_unrelated_target_does_not_affect_pre() -> None:
    """Adjustment targeting 'something_else' doesn't change pre."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="some_other_field",
        delta=-0.5,
        reason="unrelated",
        source_module="x",
    ))
    pre, _ = compute_combined_score(event, profile)
    assert pre == 0.5  # unchanged


def test_uoa_score_adjustment_still_works_back_compat() -> None:
    """The existing M34-style uoa_score rail is unaffected."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="uoa_score",
        delta=0.2,  # M34-style positive bonus
        reason="ISO sweep + cluster",
        source_module="m34_sweep_block",
    ))
    pre, _ = compute_combined_score(event, profile)
    # uoa_score bumped by 0.2; weight w.uoa = 0.30 (default)
    # Δ pre = 0.2 * 0.30 = 0.06
    assert abs(pre - (0.5 + 0.2 * 0.30)) < 1e-9


def test_penalty_engine_applies_after_combined_score_pre_adjustment() -> None:
    """ScoreAdjustment to pre + AppliedPenalty subtract correctly."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    # M24-style adjustment to pre
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.3,
        reason="M24 penalty",
        source_module="m24_iv_exhaustion",
    ))
    # Module 36-style penalty
    event.applied_penalties.append(AppliedPenalty(
        name="post_event",
        value=-0.20,
        reason="Module 36 post-event",
    ))
    pre, post = compute_combined_score(event, profile)
    # pre = 0.5 - 0.3 = 0.2
    assert abs(pre - 0.2) < 1e-9
    # post = pre + penalty = 0.2 - 0.2 = 0.0
    assert abs(post - 0.0) < 1e-9


def test_combined_score_pre_adjustment_combined_with_uoa_adjustment() -> None:
    """M34-style uoa adjustment and M24-style combined adjustment both apply."""
    profile = load_default_profile()
    event = _make_fully_scored_event()
    event.score_adjustments.append(ScoreAdjustment(
        target="uoa_score",
        delta=0.2,
        reason="M34 sweep bonus",
        source_module="m34_sweep_block",
    ))
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.4,
        reason="M24 post-earnings IV spike",
        source_module="m24_iv_exhaustion",
    ))
    pre, _ = compute_combined_score(event, profile)
    # Baseline = 0.5; uoa adj +0.2 weighted by 0.30 = +0.06;
    # combined adj -0.4 = -0.4
    # pre = 0.5 + 0.06 - 0.4 = 0.16
    expected = 0.5 + 0.2 * 0.30 - 0.4
    assert abs(pre - expected) < 1e-9
