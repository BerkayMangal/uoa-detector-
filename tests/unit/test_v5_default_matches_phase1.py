"""``profiles/v5_default.yaml`` must reproduce every Phase 1 number bit-for-bit.

This file is the single canonical "v5_default ↔ Phase 1 numbers" oracle. If
the spec changes, update this test alongside the YAML — and the divergence
will be obvious in the diff.

The intent: a developer can read one file to confirm the calibration layer
hasn't drifted from the v5 paper spec.
"""

from __future__ import annotations

import math
from datetime import time

from uoa_detector.calibration import load_default_profile


def test_combined_score_weights_match_phase1() -> None:
    s = load_default_profile().scoring
    assert math.isclose(s.uoa, 0.30)
    assert math.isclose(s.convexity, 0.25)
    assert math.isclose(s.event, 0.15)
    assert math.isclose(s.gamma, 0.10)
    assert math.isclose(s.price_confirmation, 0.10)
    assert math.isclose(s.sector_confirmation, 0.05)
    assert math.isclose(s.time_of_day, 0.05)


def test_early_scoring_weights_match_phase1() -> None:
    e = load_default_profile().early_scoring
    assert math.isclose(e.convexity, 0.40)
    assert math.isclose(e.cluster_density, 0.20)
    assert math.isclose(e.event, 0.15)
    assert math.isclose(e.gamma, 0.15)
    assert math.isclose(e.time_of_day, 0.10)


def test_penalties_match_phase1() -> None:
    p = load_default_profile().penalties
    assert math.isclose(p.post_gap_move, -0.20)
    assert math.isclose(p.iv_rank_high, -0.15)
    assert math.isclose(p.wide_spread, -0.15)
    assert math.isclose(p.thin_oi, -0.20)
    assert math.isclose(p.flow_contradicts_price, -0.15)
    assert math.isclose(p.post_event, -0.25)
    assert math.isclose(p.isolated_print, -0.10)
    assert math.isclose(p.next_day_oi_failed, -0.20)


def test_dte_multipliers_match_phase1() -> None:
    d = load_default_profile().dte
    assert math.isclose(d.bucket_0_7, 1.20)
    assert math.isclose(d.bucket_8_14, 1.10)
    assert math.isclose(d.bucket_15_30, 1.00)
    assert math.isclose(d.bucket_31_60, 0.85)
    assert math.isclose(d.bucket_60_plus, 0.70)
    assert d.leap_threshold == 90


def test_time_of_day_windows_match_phase1_via_lookup() -> None:
    """Spec table reproduced via the new windows list.

    Each (window_label, weight) pair below comes directly from
    UOA_Convexity_Detector_v5.docx Module 39.
    """
    tod = load_default_profile().time_of_day
    assert tod.timezone == "America/New_York"

    cases: list[tuple[time, float, str]] = [
        # 09:30–10:00 → 0.30 open_auction
        (time(9, 35), 0.30, "open_auction"),
        # 10:00–11:00 → 0.80 early_session
        (time(10, 30), 0.80, "early_session"),
        # 11:00–14:00 → 1.00 prime_session
        (time(11, 30), 1.00, "prime_session"),
        (time(13, 0), 1.00, "prime_session"),
        # 14:00–15:30 → 0.70 afternoon
        (time(14, 30), 0.70, "afternoon"),
        # 15:30–16:00 → 0.30 moc_loc
        (time(15, 45), 0.30, "moc_loc"),
    ]
    for t, expected_weight, expected_label in cases:
        weight, label = tod.lookup(t)
        assert math.isclose(weight, expected_weight), f"weight mismatch at {t}"
        assert label == expected_label, f"label mismatch at {t}"


def test_outside_session_weight_matches_phase1() -> None:
    """outside_session_weight must reproduce Phase 1's extended_hours: 0.30.

    The v5 spec is silent on outside-session weighting; Phase 1 chose 0.30
    (its lowest in-session weight) and we preserve that here for bit-for-bit
    reproduction. Override per-ticker via deep-merge if a different policy
    is desired. The ``extended_hours_policy`` field surfaces this gap
    explicitly; see ``test_extended_hours_policy_default``.
    """
    assert math.isclose(load_default_profile().time_of_day.outside_session_weight, 0.30)


def test_extended_hours_policy_default() -> None:
    """v5_default uses 'flag' policy: numerical Phase 1 behavior + visible tag.

    Per Phase 2.3.1, the spec-silent extended-hours behavior is structured into
    a typed enum so the gap is visible in every record rather than buried in a
    single number. 'flag' is the least-surprising default — it keeps Phase 1's
    weight semantics and adds a boolean flag to the decision record.
    """
    assert load_default_profile().time_of_day.extended_hours_policy == "flag"


def test_cluster_match_phase1() -> None:
    c = load_default_profile().cluster
    assert c.window_minutes == 60
    assert c.decay_minutes == 90
    assert c.nearby_strike_count == 1
    assert math.isclose(c.escalation_ratio, 1.20)
    assert math.isclose(c.density_isolated, 0.0)
    assert math.isclose(c.density_two_same, 0.4)
    assert math.isclose(c.density_three_same, 0.8)
    assert math.isclose(c.density_three_escalating, 1.0)
    assert math.isclose(c.density_nearby_strikes, 0.6)


def test_sweep_match_phase1() -> None:
    s = load_default_profile().sweep
    assert math.isclose(s.iso_bonus, 0.15)
    assert math.isclose(s.cross_venue_bonus, 0.10)
    assert math.isclose(s.cross_venue_above_ask_bonus, 0.15)


def test_relative_premium_match_phase1() -> None:
    r = load_default_profile().relative_premium
    assert math.isclose(r.high_ratio, 3.0)
    assert math.isclose(r.mid_ratio, 1.5)
    assert math.isclose(r.score_high, 1.0)
    assert math.isclose(r.score_mid, 0.5)
    assert math.isclose(r.score_low, 0.0)
    assert r.median_window_days == 30


def test_label_thresholds_match_phase1() -> None:
    t = load_default_profile().label_thresholds
    assert math.isclose(t.penalized_below, 0.30)
    assert math.isclose(t.cluster_min, 0.4)
    assert math.isclose(t.cluster_burst, 0.8)
    assert math.isclose(t.relative_premium_uoa, 0.5)
    assert math.isclose(t.convexity_watch_floor, 0.6)
    assert math.isclose(t.pre_catalyst_event_min, 0.75)


def test_risk_buckets_match_phase1() -> None:
    r = load_default_profile().risk_buckets
    assert math.isclose(r.convexity_watch, 0.25)
    assert math.isclose(r.convexity_cluster, 0.50)
    assert math.isclose(r.standard_uoa, 0.75)
    assert math.isclose(r.sweep_uoa, 0.85)
    assert math.isclose(r.pre_catalyst_flow, 0.85)
    assert math.isclose(r.high_conviction_sequence, 1.00)
    assert math.isclose(r.high_conviction_initial, 0.50)
    assert math.isclose(r.leap_positioning, 0.25)
    assert math.isclose(r.discard_or_log, 0.0)
    # Phase 2.3.2a — REJECTED bucket; structurally distinct from discard.
    assert math.isclose(r.rejected, 0.0)


def test_penalty_triggers_match_phase1() -> None:
    t = load_default_profile().penalty_triggers
    assert math.isclose(t.gap_threshold_pct, 3.0)
    assert t.iv_rank_threshold == 80
    assert math.isclose(t.spread_pct_threshold, 15.0)
    assert t.thin_oi_threshold == 100
    assert t.isolated_window_min == 30
    assert math.isclose(t.next_day_oi_drop_pct, 30.0)
    assert t.post_event_sessions == 2


def test_fusion_phase2_defaults() -> None:
    """fusion is a Phase 2 addition; values per the agreed defaults."""
    f = load_default_profile().fusion
    assert f.window_ms == 500
    assert f.timestamp_skew_tolerance_ms == 100


def test_sub_score_missing_behavior_uses_zero_not_half() -> None:
    """Per the user correction: missing data is 0.0 (no signal), not 0.5."""
    b = load_default_profile().sub_score_missing_behavior
    for name in (
        "uoa_score", "convexity_score", "event_score", "gamma_score",
        "price_confirmation_score", "sector_confirmation_score",
        "cluster_density_score", "relative_premium_score",
    ):
        policy = getattr(b, name)
        assert policy.on_missing == "default"
        assert math.isclose(policy.default, 0.0)
    # time_of_day_weight is always derivable from timestamp; None means bug
    assert b.time_of_day_weight.on_missing == "raise"
