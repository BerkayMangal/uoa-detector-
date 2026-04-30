"""Unit tests for the v5 combined-score formula and DTE multiplier handling."""

from __future__ import annotations

import math

import pytest

from tests.conftest import build_enriched
from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import AppliedPenalty
from uoa_detector.errors import MissingSubScoreError, ScoreOutOfRangeError
from uoa_detector.scoring.combined import (
    compute_combined_score,
    compute_early_convexity_score,
)


def _hand_combined(
    *,
    uoa: float,
    convexity: float,
    event_s: float,
    gamma: float,
    price: float,
    sector: float,
    tod: float,
    multiplier: float,
    profile: CalibrationProfile,
) -> float:
    """Compute the v5 combined score by hand for a test oracle."""
    w = profile.scoring
    return (
        w.uoa * uoa
        + w.convexity * convexity * multiplier
        + w.event * event_s
        + w.gamma * gamma * multiplier
        + w.price_confirmation * price
        + w.sector_confirmation * sector
        + w.time_of_day * tod
    )


def test_all_neutral_no_penalty_matches_hand_calculation() -> None:
    profile = load_default_profile()
    event = build_enriched(
        uoa=0.5, convexity=0.5, event_score=0.5, gamma=0.5,
        price=0.5, sector=0.5, tod=1.0, dte_multiplier=1.0,
    )
    pre, post = compute_combined_score(event, profile)

    expected = _hand_combined(
        uoa=0.5, convexity=0.5, event_s=0.5, gamma=0.5,
        price=0.5, sector=0.5, tod=1.0, multiplier=1.0, profile=profile,
    )
    # 0.30*0.5 + 0.25*0.5 + 0.15*0.5 + 0.10*0.5 + 0.10*0.5 + 0.05*0.5 + 0.05*1.0
    # = 0.15 + 0.125 + 0.075 + 0.05 + 0.05 + 0.025 + 0.05 = 0.525
    assert math.isclose(pre, 0.525, abs_tol=1e-9)
    assert math.isclose(pre, expected, abs_tol=1e-9)
    assert pre == post  # No penalties applied
    assert event.combined_score_pre_penalty == pre
    assert event.combined_score_post_penalty == post


def test_dte_multiplier_applies_only_to_convexity_and_gamma() -> None:
    """0–7 DTE bucket = 1.20×; multiplier scales convexity and gamma weights."""
    profile = load_default_profile()
    event = build_enriched(
        uoa=0.4, convexity=0.6, event_score=0.5, gamma=0.5,
        price=0.5, sector=0.5, tod=1.0, dte_multiplier=1.20,
    )
    pre, _ = compute_combined_score(event, profile)

    expected = _hand_combined(
        uoa=0.4, convexity=0.6, event_s=0.5, gamma=0.5,
        price=0.5, sector=0.5, tod=1.0, multiplier=1.20, profile=profile,
    )
    assert math.isclose(pre, expected, abs_tol=1e-9)


def test_combined_score_with_penalties_subtracts() -> None:
    profile = load_default_profile()
    event = build_enriched(
        uoa=0.5, convexity=0.5, event_score=0.5, gamma=0.5,
        price=0.5, sector=0.5, tod=1.0, dte_multiplier=1.0,
    )
    event.applied_penalties.append(
        AppliedPenalty(name="post_event", value=-0.25, reason="test")
    )
    event.applied_penalties.append(
        AppliedPenalty(name="thin_oi", value=-0.20, reason="test")
    )
    pre, post = compute_combined_score(event, profile)

    assert math.isclose(pre, 0.525, abs_tol=1e-9)
    assert math.isclose(post, 0.525 - 0.25 - 0.20, abs_tol=1e-9)


def test_missing_sub_score_raises_in_strict_mode() -> None:
    """Strict mode raises on ANY None sub-score (Phase 1 behavior preserved)."""
    profile = load_default_profile()
    event = build_enriched(uoa=0.5)
    event.convexity_score = None  # Simulate missing stage output

    with pytest.raises(MissingSubScoreError) as info:
        compute_combined_score(event, profile, strict=True)
    assert info.value.name == "convexity_score"


def test_missing_sub_score_uses_default_in_default_mode() -> None:
    """Default mode substitutes profile-configured default and records the field."""
    profile = load_default_profile()
    event = build_enriched()
    event.convexity_score = None  # v5_default policy: default=0.0

    # Should NOT raise — default-substitution kicks in
    pre, _ = compute_combined_score(event, profile)
    assert "convexity_score" in event.missing_sub_scores
    # convexity contribution = w.convexity * 0.0 * dte_multiplier = 0.0
    # so pre is the rest of the formula
    expected_no_convexity = (
        0.30 * 0.5 + 0.25 * 0.0 + 0.15 * 0.5 + 0.10 * 0.5
        + 0.10 * 0.5 + 0.05 * 0.5 + 0.05 * 1.0
    )
    assert math.isclose(pre, expected_no_convexity, abs_tol=1e-9)


def test_missing_time_of_day_weight_always_raises() -> None:
    """time_of_day_weight is always derivable from timestamp; None means a bug.

    Per the v5_default profile, its on_missing policy is 'raise' even outside
    strict mode.
    """
    profile = load_default_profile()
    event = build_enriched()
    event.time_of_day_weight = None

    with pytest.raises(MissingSubScoreError) as info:
        compute_combined_score(event, profile)
    assert info.value.name == "time_of_day_weight"


def test_out_of_range_sub_score_raises() -> None:
    profile = load_default_profile()
    event = build_enriched()
    event.uoa_score = 1.5  # Out of [0,1]

    with pytest.raises(ScoreOutOfRangeError):
        compute_combined_score(event, profile)


def test_missing_dte_multiplier_raises() -> None:
    profile = load_default_profile()
    event = build_enriched()
    event.dte_multiplier_applied = None

    with pytest.raises(MissingSubScoreError) as info:
        compute_combined_score(event, profile)
    assert info.value.name == "dte_multiplier_applied"


def test_early_convexity_score_matches_hand_calc() -> None:
    profile = load_default_profile()
    event = build_enriched(
        convexity=0.7, cluster=0.4, event_score=0.5,
        gamma=0.6, tod=1.0,
    )
    val = compute_early_convexity_score(event, profile)
    expected = 0.40 * 0.7 + 0.20 * 0.4 + 0.15 * 0.5 + 0.15 * 0.6 + 0.10 * 1.0
    # 0.28 + 0.08 + 0.075 + 0.09 + 0.10 = 0.625
    assert math.isclose(val, 0.625, abs_tol=1e-9)
    assert math.isclose(val, expected, abs_tol=1e-9)


def test_v5_weight_sum_invariant() -> None:
    """Combined-score weights (excluding penalties) must sum to 1.0 in v5."""
    w = load_default_profile().scoring
    total = (
        w.uoa + w.convexity + w.event + w.gamma
        + w.price_confirmation + w.sector_confirmation + w.time_of_day
    )
    assert math.isclose(total, 1.0, abs_tol=1e-9)


def test_early_convexity_weight_sum_invariant() -> None:
    """Early-convexity weights must sum to 1.0 (Part 3 of the spec)."""
    ew = load_default_profile().early_scoring
    total = ew.convexity + ew.cluster_density + ew.event + ew.gamma + ew.time_of_day
    assert math.isclose(total, 1.0, abs_tol=1e-9)
