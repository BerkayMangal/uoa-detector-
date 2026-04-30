"""Tests that ``profiles/v5_default.yaml`` reproduces every Phase 1 number bit-for-bit.

If any of these assertions fail, you have either:
  (a) edited a v5 number in the YAML by mistake (revert it), or
  (b) intentionally changed the spec (then update the spec doc and the test
      together).
"""

from __future__ import annotations

import math

import pytest

from uoa_detector.calibration import (
    CalibrationProfile,
    load_default_profile,
)
from uoa_detector.errors import ConfigurationError


@pytest.fixture
def v5() -> CalibrationProfile:
    return load_default_profile()


def test_profile_id_and_no_inheritance(v5: CalibrationProfile) -> None:
    assert v5.profile_id == "v5_default"
    assert v5.inherits_from is None


def test_combined_score_weights(v5: CalibrationProfile) -> None:
    s = v5.scoring
    assert math.isclose(s.uoa, 0.30)
    assert math.isclose(s.convexity, 0.25)
    assert math.isclose(s.event, 0.15)
    assert math.isclose(s.gamma, 0.10)
    assert math.isclose(s.price_confirmation, 0.10)
    assert math.isclose(s.sector_confirmation, 0.05)
    assert math.isclose(s.time_of_day, 0.05)


def test_combined_score_weights_sum_to_one(v5: CalibrationProfile) -> None:
    s = v5.scoring
    total = (
        s.uoa + s.convexity + s.event + s.gamma
        + s.price_confirmation + s.sector_confirmation + s.time_of_day
    )
    assert math.isclose(total, 1.0, abs_tol=1e-9)


def test_early_scoring_weights(v5: CalibrationProfile) -> None:
    e = v5.early_scoring
    assert math.isclose(e.convexity, 0.40)
    assert math.isclose(e.cluster_density, 0.20)
    assert math.isclose(e.event, 0.15)
    assert math.isclose(e.gamma, 0.15)
    assert math.isclose(e.time_of_day, 0.10)


def test_module_36_penalties_exact_values(v5: CalibrationProfile) -> None:
    p = v5.penalties
    assert math.isclose(p.post_gap_move, -0.20)
    assert math.isclose(p.iv_rank_high, -0.15)
    assert math.isclose(p.wide_spread, -0.15)
    assert math.isclose(p.thin_oi, -0.20)
    assert math.isclose(p.flow_contradicts_price, -0.15)
    assert math.isclose(p.post_event, -0.25)
    assert math.isclose(p.isolated_print, -0.10)
    assert math.isclose(p.next_day_oi_failed, -0.20)


def test_penalty_triggers(v5: CalibrationProfile) -> None:
    t = v5.penalty_triggers
    assert math.isclose(t.gap_threshold_pct, 3.0)
    assert t.iv_rank_threshold == 80
    assert math.isclose(t.spread_pct_threshold, 15.0)
    assert t.thin_oi_threshold == 100
    assert t.isolated_window_min == 30
    assert math.isclose(t.next_day_oi_drop_pct, 30.0)
    assert t.post_event_sessions == 2


def test_dte_multipliers(v5: CalibrationProfile) -> None:
    d = v5.dte
    assert math.isclose(d.bucket_0_7, 1.20)
    assert math.isclose(d.bucket_8_14, 1.10)
    assert math.isclose(d.bucket_15_30, 1.00)
    assert math.isclose(d.bucket_31_60, 0.85)
    assert math.isclose(d.bucket_60_plus, 0.70)
    assert d.leap_threshold == 90


def test_time_of_day_weights_via_lookup(v5: CalibrationProfile) -> None:
    """v5_default reproduces the spec's window weights via the new lookup API."""
    from datetime import time as _time

    tod = v5.time_of_day
    assert tod.timezone == "America/New_York"
    assert math.isclose(tod.outside_session_weight, 0.30)

    # One representative point inside each window
    assert tod.lookup(_time(9, 45)) == (0.30, "open_auction")
    assert tod.lookup(_time(10, 30)) == (0.80, "early_session")
    assert tod.lookup(_time(12, 0)) == (1.00, "prime_session")
    assert tod.lookup(_time(15, 0)) == (0.70, "afternoon")
    assert tod.lookup(_time(15, 45)) == (0.30, "moc_loc")

    # Boundary: end is exclusive — 16:00 is outside the session
    assert tod.lookup(_time(16, 0)) == (0.30, "outside_session")
    # Pre-market / after-hours
    assert tod.lookup(_time(8, 0)) == (0.30, "outside_session")
    assert tod.lookup(_time(20, 0)) == (0.30, "outside_session")


def test_cluster_params(v5: CalibrationProfile) -> None:
    c = v5.cluster
    assert c.window_minutes == 60
    assert c.decay_minutes == 90
    assert c.nearby_strike_count == 1
    assert math.isclose(c.escalation_ratio, 1.20)
    assert math.isclose(c.density_isolated, 0.0)
    assert math.isclose(c.density_two_same, 0.4)
    assert math.isclose(c.density_three_same, 0.8)
    assert math.isclose(c.density_three_escalating, 1.0)
    assert math.isclose(c.density_nearby_strikes, 0.6)


def test_sweep_params(v5: CalibrationProfile) -> None:
    s = v5.sweep
    assert math.isclose(s.iso_bonus, 0.15)
    assert math.isclose(s.cross_venue_bonus, 0.10)
    assert math.isclose(s.cross_venue_above_ask_bonus, 0.15)


def test_relative_premium_params(v5: CalibrationProfile) -> None:
    r = v5.relative_premium
    assert math.isclose(r.high_ratio, 3.0)
    assert math.isclose(r.mid_ratio, 1.5)
    assert math.isclose(r.score_high, 1.0)
    assert math.isclose(r.score_mid, 0.5)
    assert math.isclose(r.score_low, 0.0)
    assert r.median_window_days == 30


def test_label_thresholds(v5: CalibrationProfile) -> None:
    t = v5.label_thresholds
    assert math.isclose(t.penalized_below, 0.30)
    assert math.isclose(t.cluster_min, 0.4)
    assert math.isclose(t.cluster_burst, 0.8)
    assert math.isclose(t.relative_premium_uoa, 0.5)
    assert math.isclose(t.convexity_watch_floor, 0.6)
    assert math.isclose(t.pre_catalyst_event_min, 0.75)


def test_risk_buckets(v5: CalibrationProfile) -> None:
    r = v5.risk_buckets
    assert math.isclose(r.convexity_watch, 0.25)
    assert math.isclose(r.convexity_cluster, 0.50)
    assert math.isclose(r.standard_uoa, 0.75)
    assert math.isclose(r.sweep_uoa, 0.85)
    assert math.isclose(r.pre_catalyst_flow, 0.85)
    assert math.isclose(r.high_conviction_sequence, 1.00)
    assert math.isclose(r.high_conviction_initial, 0.50)
    assert math.isclose(r.leap_positioning, 0.25)
    assert math.isclose(r.discard_or_log, 0.0)
    assert math.isclose(r.rejected, 0.0)  # Phase 2.3.2a: distinct from discard_or_log


def test_fusion_defaults(v5: CalibrationProfile) -> None:
    f = v5.fusion
    assert f.window_ms == 500
    assert f.timestamp_skew_tolerance_ms == 100
    # Phase 2.3.3 watermark tunables
    assert f.stalled_source_timeout_ms == 2000
    assert f.allowed_lateness_ms == 200
    # Phase 2.3.2 confidence-tier resolution thresholds
    assert f.tier_thresholds.unanimous_min_sources == 2
    assert math.isclose(f.tier_thresholds.majority_fraction, 0.5)
    assert math.isclose(f.tier_thresholds.premium_disagreement_tolerance_pct, 0.05)


def test_sub_score_missing_behavior_defaults_to_zero_not_half(v5: CalibrationProfile) -> None:
    """Per the user's correction: missing data is 0.0 (no signal), not 0.5."""
    b = v5.sub_score_missing_behavior
    for name in (
        "uoa_score", "convexity_score", "event_score", "gamma_score",
        "price_confirmation_score", "sector_confirmation_score",
        "cluster_density_score", "relative_premium_score",
    ):
        policy = getattr(b, name)
        assert policy.on_missing == "default"
        assert math.isclose(policy.default, 0.0), (
            f"{name} default must be 0.0 (truly neutral / no signal). "
            f"0.5 is NOT neutral for several sub-scores — it's a positive signal."
        )

    # time_of_day_weight is always derivable from the timestamp; None means bug
    assert b.time_of_day_weight.on_missing == "raise"


def test_invalid_weights_sum_rejected() -> None:
    """If you accidentally edit weights so they don't sum to 1.0, validation fails."""
    bad: dict = {
        "profile_id": "broken",
        "description": "x",
        "inherits_from": None,
        "scoring": {
            "uoa": 0.99, "convexity": 0.0, "event": 0.0, "gamma": 0.0,
            "price_confirmation": 0.0, "sector_confirmation": 0.0, "time_of_day": 0.0,
        },
        "early_scoring": {
            "convexity": 0.4, "cluster_density": 0.2, "event": 0.15,
            "gamma": 0.15, "time_of_day": 0.1,
        },
        "penalties": {
            "post_gap_move": -0.2, "iv_rank_high": -0.15, "wide_spread": -0.15,
            "thin_oi": -0.2, "flow_contradicts_price": -0.15, "post_event": -0.25,
            "isolated_print": -0.1, "next_day_oi_failed": -0.2,
        },
        "penalty_triggers": {
            "gap_threshold_pct": 3.0, "iv_rank_threshold": 80, "spread_pct_threshold": 15.0,
            "thin_oi_threshold": 100, "isolated_window_min": 30,
            "next_day_oi_drop_pct": 30.0, "post_event_sessions": 2,
        },
        "contradiction": {"magnitude": -0.15, "enabled": True},
        "dte": {
            "bucket_0_7": 1.2, "bucket_8_14": 1.1, "bucket_15_30": 1.0,
            "bucket_31_60": 0.85, "bucket_60_plus": 0.7, "leap_threshold": 90,
        },
        "time_of_day": {
            "timezone": "America/New_York",
            "windows": [
                {"start": "09:30", "end": "10:00", "weight": 0.30, "label": "open_auction"},
                {"start": "10:00", "end": "11:00", "weight": 0.80, "label": "early_session"},
                {"start": "11:00", "end": "14:00", "weight": 1.00, "label": "prime_session"},
                {"start": "14:00", "end": "15:30", "weight": 0.70, "label": "afternoon"},
                {"start": "15:30", "end": "16:00", "weight": 0.30, "label": "moc_loc"},
            ],
            "outside_session_weight": 0.0,
        },
        "cluster": {
            "window_minutes": 60, "decay_minutes": 90, "nearby_strike_count": 1,
            "escalation_ratio": 1.2,
            "density_isolated": 0.0, "density_two_same": 0.4,
            "density_three_same": 0.8, "density_three_escalating": 1.0,
            "density_nearby_strikes": 0.6,
        },
        "sweep": {
            "iso_bonus": 0.15, "cross_venue_bonus": 0.1,
            "cross_venue_above_ask_bonus": 0.15, "cross_venue_window_ms": 50,
        },
        "relative_premium": {
            "high_ratio": 3.0, "mid_ratio": 1.5, "score_high": 1.0,
            "score_mid": 0.5, "score_low": 0.0, "median_window_days": 30,
        },
        "label_thresholds": {
            "penalized_below": 0.3, "cluster_min": 0.4, "cluster_burst": 0.8,
            "relative_premium_uoa": 0.5, "convexity_watch_floor": 0.6,
            "pre_catalyst_event_min": 0.75,
        },
        "risk_buckets": {
            "convexity_watch": 0.25, "convexity_cluster": 0.5, "standard_uoa": 0.75,
            "sweep_uoa": 0.85, "pre_catalyst_flow": 0.85,
            "high_conviction_sequence": 1.0, "high_conviction_initial": 0.5,
            "leap_positioning": 0.25, "discard_or_log": 0.0, "rejected": 0.0,
        },
        "fusion": {
            "window_ms": 500,
            "timestamp_skew_tolerance_ms": 100,
            "stalled_source_timeout_ms": 2000,
            "allowed_lateness_ms": 200,
            "tier_thresholds": {
                "unanimous_min_sources": 2,
                "majority_fraction": 0.5,
                "premium_disagreement_tolerance_pct": 0.05,
            },
        },
        "sub_score_missing_behavior": {
            n: {"on_missing": "default", "default": 0.0}
            for n in (
                "uoa_score", "convexity_score", "event_score", "gamma_score",
                "price_confirmation_score", "sector_confirmation_score",
                "cluster_density_score", "relative_premium_score",
            )
        } | {"time_of_day_weight": {"on_missing": "raise"}},
    }
    with pytest.raises(Exception, match="scoring weights must sum"):
        CalibrationProfile.model_validate(bad)


def test_unknown_keys_rejected(v5: CalibrationProfile, tmp_path) -> None:
    """Strict-mode validation rejects unknown keys — typo protection."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "profile_id: bad\ndescription: x\ninherits_from: v5_default\n"
        "scoring: { uoa: 0.30, convexity: 0.25, event: 0.15, gamma: 0.10,"
        "  price_confirmation: 0.10, sector_confirmation: 0.05, time_of_day: 0.05,"
        "  this_is_not_a_real_field: 0.42 }\n"
    )
    # Need v5_default in tmp_path to resolve inherits_from
    import shutil
    from pathlib import Path

    from uoa_detector.calibration.loader import load_profile
    src_default = Path("profiles/v5_default.yaml").resolve()
    shutil.copy(src_default, tmp_path / "v5_default.yaml")

    with pytest.raises(ConfigurationError):
        load_profile(bad_yaml, profiles_dir=tmp_path)
