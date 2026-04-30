"""Unit tests for the risk sizer — every label maps to its v5 max-R."""

from __future__ import annotations

import math

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.domain.risk import RiskBucket
from uoa_detector.risk.sizer import RiskSizer

_EXPECTED_R: dict[SignalLabel, float] = {
    SignalLabel.CONVEXITY_WATCH: 0.25,
    SignalLabel.CONVEXITY_CLUSTER: 0.50,
    SignalLabel.CONVEXITY_BURST: 0.50,
    SignalLabel.STANDARD_UOA: 0.75,
    SignalLabel.SWEEP_UOA: 0.85,
    SignalLabel.PRE_CATALYST_FLOW: 0.85,
    SignalLabel.HIGH_CONVICTION_SEQUENCE: 1.00,
    SignalLabel.LEAP_POSITIONING: 0.25,
    # Discard / log / additive labels
    SignalLabel.IGNORE_NOISE: 0.0,
    SignalLabel.LIKELY_CLOSING_OR_NOISE: 0.0,
    SignalLabel.POST_EVENT_NOISE: 0.0,
    SignalLabel.PENALIZED_BELOW_THRESHOLD: 0.0,
    SignalLabel.OPENING_UNCONFIRMED: 0.0,
    SignalLabel.SECTOR_FLOW_CLUSTER: 0.0,
    SignalLabel.OPTIONS_EQUITY_TAPE_CONFIRMATION: 0.0,
    SignalLabel.CONFIRMED_OPENING_FLOW: 0.0,
    SignalLabel.GAMMA_ACCELERATION_RISK: 0.0,
    # Phase 2.3.2a — REJECTED has its own bucket (max_r=0, but distinct from DISCARD)
    SignalLabel.REJECTED: 0.0,
}


@pytest.mark.parametrize(("label", "expected_r"), list(_EXPECTED_R.items()))
def test_each_label_maps_to_correct_r(label: SignalLabel, expected_r: float) -> None:
    sizer = RiskSizer(load_default_profile())
    size = sizer.size_for(label)
    assert math.isclose(size.max_r, expected_r, abs_tol=1e-9)


def test_high_conviction_sequence_uses_scale_in_with_initial_50pct() -> None:
    size = RiskSizer(load_default_profile()).size_for(SignalLabel.HIGH_CONVICTION_SEQUENCE)
    assert size.scale_in is True
    assert size.initial_r is not None
    assert math.isclose(size.initial_r, 0.50, abs_tol=1e-9)
    assert size.bucket == RiskBucket.HIGH_CONVICTION_SEQUENCE


def test_other_labels_do_not_scale_in() -> None:
    sizer = RiskSizer(load_default_profile())
    for label in SignalLabel:
        if label == SignalLabel.HIGH_CONVICTION_SEQUENCE:
            continue
        size = sizer.size_for(label)
        assert size.scale_in is False
        assert size.initial_r is None


def test_buckets_for_tradeable_labels() -> None:
    sizer = RiskSizer(load_default_profile())
    pairs: list[tuple[SignalLabel, RiskBucket]] = [
        (SignalLabel.CONVEXITY_WATCH, RiskBucket.CONVEXITY_WATCH),
        (SignalLabel.CONVEXITY_CLUSTER, RiskBucket.CONVEXITY_CLUSTER),
        (SignalLabel.CONVEXITY_BURST, RiskBucket.CONVEXITY_CLUSTER),
        (SignalLabel.STANDARD_UOA, RiskBucket.STANDARD_UOA),
        (SignalLabel.SWEEP_UOA, RiskBucket.SWEEP_UOA),
        (SignalLabel.PRE_CATALYST_FLOW, RiskBucket.PRE_CATALYST_FLOW),
        (SignalLabel.LEAP_POSITIONING, RiskBucket.LEAP_POSITIONING),
        (SignalLabel.HIGH_CONVICTION_SEQUENCE, RiskBucket.HIGH_CONVICTION_SEQUENCE),
    ]
    for label, expected_bucket in pairs:
        assert sizer.size_for(label).bucket == expected_bucket


def test_rejected_uses_distinct_bucket_not_discard() -> None:
    """Phase 2.3.2a: REJECTED must map to RiskBucket.REJECTED, not DISCARD.

    This is the whole point of separating the two — backtest analysis can
    distinguish 'evaluated, no signal' (DISCARD) from 'not evaluated, out
    of scope' (REJECTED).
    """
    sizer = RiskSizer(load_default_profile())
    rejected = sizer.size_for(SignalLabel.REJECTED)
    discard = sizer.size_for(SignalLabel.IGNORE_NOISE)
    assert rejected.bucket == RiskBucket.REJECTED
    assert discard.bucket == RiskBucket.DISCARD
    assert rejected.bucket != discard.bucket


def test_every_label_in_enum_is_covered_by_expected_table() -> None:
    """If a future spec adds a label, this test forces the table to update."""
    assert set(_EXPECTED_R.keys()) == set(SignalLabel)
