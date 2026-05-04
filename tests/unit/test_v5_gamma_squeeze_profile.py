"""Pin tests for ``profiles/v5_gamma_squeeze.yaml``.

Every value in this file is a deliberate calibration decision for Track B
(Gamma Squeeze Precursor) + Formülasyon A (multi-source fusion × Tier-2
universe edge). If any of these tests fails, an override has been changed
unintentionally — review the profile file and ``CALIBRATION.md`` before
"fixing" the test.

Inheritance check: every leaf NOT in the override list must equal v5_default's
value. This catches:
  - leaf typos (override key misspelled, value silently inherited)
  - silent contract drift (someone adds a new field to v5_default that
    needs a Track B override but didn't get one)

Override check: every leaf IN the override list must equal the planned
Track B value.

Source of truth for what's overridden: see profiles/v5_gamma_squeeze.yaml
itself. Override decisions documented in:
  - The Phase 3 prep conversation (5-step preparation)
  - CALIBRATION.md (calibration surface)
  - profile commit message ('Phase 3.0.1: v5_gamma_squeeze.yaml')
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uoa_detector.calibration.loader import load_profile

_PROFILES_DIR = Path("profiles")


@pytest.fixture(scope="module")
def gamma_squeeze_profile():  # type: ignore[no-untyped-def]
    """Load the Track B profile once per test module."""
    return load_profile(_PROFILES_DIR / "v5_gamma_squeeze.yaml")


@pytest.fixture(scope="module")
def default_profile():  # type: ignore[no-untyped-def]
    """Load v5_default for inheritance comparison."""
    return load_profile(_PROFILES_DIR / "v5_default.yaml")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_profile_id_is_gamma_squeeze(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    assert gamma_squeeze_profile.profile_id == "v5_gamma_squeeze"


def test_inherits_from_default(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    assert gamma_squeeze_profile.inherits_from == "v5_default"


def test_content_hash_differs_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """The fork must produce a different audit hash from v5_default;
    decision records can then distinguish which profile produced a signal.
    """
    assert gamma_squeeze_profile.content_hash() != default_profile.content_hash()


# ---------------------------------------------------------------------------
# Override: scoring weights (combined-score formula)
# ---------------------------------------------------------------------------


def test_scoring_weights_track_b_distribution(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Track B reweights toward gamma + convexity, away from event."""
    sc = gamma_squeeze_profile.scoring
    assert sc.uoa == 0.20                   # was 0.30
    assert sc.convexity == 0.30             # was 0.25
    assert sc.event == 0.05                 # was 0.15
    assert sc.gamma == 0.25                 # was 0.10  (largest shift)
    assert sc.price_confirmation == 0.10    # unchanged
    assert sc.sector_confirmation == 0.05   # unchanged
    assert sc.time_of_day == 0.05           # unchanged


def test_scoring_weights_sum_to_one(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Validator invariant — must hold for any profile, doubly verified here."""
    sc = gamma_squeeze_profile.scoring
    total = (
        sc.uoa + sc.convexity + sc.event + sc.gamma
        + sc.price_confirmation + sc.sector_confirmation + sc.time_of_day
    )
    assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Override: early_scoring weights
# ---------------------------------------------------------------------------


def test_early_scoring_weights_lead_with_gamma_and_cluster(
    gamma_squeeze_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Pre-UOA scoring leans on gamma + cluster_density for Track B."""
    es = gamma_squeeze_profile.early_scoring
    assert es.convexity == 0.35             # was 0.40
    assert es.cluster_density == 0.25       # was 0.20
    assert es.event == 0.05                 # was 0.15
    assert es.gamma == 0.30                 # was 0.15  (lead indicator)
    assert es.time_of_day == 0.05           # was 0.10


def test_early_scoring_weights_sum_to_one(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    es = gamma_squeeze_profile.early_scoring
    total = es.convexity + es.cluster_density + es.event + es.gamma + es.time_of_day
    assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Override: cluster (Module 38) — tighter window, faster decay
# ---------------------------------------------------------------------------


def test_cluster_window_compressed_for_track_b(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Squeezes ignite within 30min or never — wider window adds noise."""
    cl = gamma_squeeze_profile.cluster
    assert cl.window_minutes == 30          # was 60
    assert cl.decay_minutes == 45           # was 90
    assert cl.escalation_ratio == 1.30      # was 1.20  (more aggressive)
    assert cl.nearby_strike_count == 1      # unchanged


def test_cluster_density_bands_track_b(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Reweight density bands: escalating still 1.0, others lower."""
    cl = gamma_squeeze_profile.cluster
    assert cl.density_isolated == 0.0           # unchanged
    assert cl.density_two_same == 0.3           # was 0.4
    assert cl.density_three_same == 0.7         # was 0.8
    assert cl.density_three_escalating == 1.0   # unchanged (gold standard)
    assert cl.density_nearby_strikes == 0.5     # was 0.6


# ---------------------------------------------------------------------------
# Override: sweep (Module 34) — heavier rewards for ISO + above_ask
# ---------------------------------------------------------------------------


def test_sweep_bonuses_increased_for_track_b(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    sw = gamma_squeeze_profile.sweep
    assert sw.iso_bonus == 0.20                       # was 0.15
    assert sw.cross_venue_bonus == 0.10               # unchanged
    assert sw.cross_venue_above_ask_bonus == 0.20     # was 0.15
    assert sw.cross_venue_window_ms == 50             # unchanged


# ---------------------------------------------------------------------------
# Override: DTE multipliers (Module 35) — narrow to 3-14 DTE
# ---------------------------------------------------------------------------


def test_dte_multipliers_favor_short_dte(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Track B trades 3-14 DTE OTM calls. 15+ DTE penalised steeply."""
    d = gamma_squeeze_profile.dte
    assert d.bucket_0_7 == 1.30        # was 1.20  (Track B's heart)
    assert d.bucket_8_14 == 1.20       # was 1.10
    assert d.bucket_15_30 == 0.50      # was 1.00  (steep penalty)
    assert d.bucket_31_60 == 0.30      # was 0.85
    assert d.bucket_60_plus == 0.10    # was 0.70  (no-go)
    assert d.leap_threshold == 60      # was 90    (anything 60+ is LEAP)


def test_dte_multipliers_monotonic_decreasing(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Sanity invariant: multipliers should never increase as DTE grows
    in a Track B profile (longer DTE = less squeeze relevance).
    """
    d = gamma_squeeze_profile.dte
    assert d.bucket_0_7 >= d.bucket_8_14
    assert d.bucket_8_14 >= d.bucket_15_30
    assert d.bucket_15_30 >= d.bucket_31_60
    assert d.bucket_31_60 >= d.bucket_60_plus


# ---------------------------------------------------------------------------
# Override: label_thresholds — "az ama öz" filter
# ---------------------------------------------------------------------------


def test_label_thresholds_tightened_for_az_ama_oz(
    gamma_squeeze_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Tighter bands so only genuine Track B signals pass the labeler.
    Combined with Tier-2 universe + fusion=unanimous runtime selection,
    expected output is ~30 trades/year.
    """
    lt = gamma_squeeze_profile.label_thresholds
    assert lt.penalized_below == 0.55           # was 0.30  (sharper rejection)
    assert lt.cluster_min == 0.7                # was 0.4   (demand density)
    assert lt.cluster_burst == 0.90             # was 0.8   (BURST is the prize)
    assert lt.relative_premium_uoa == 0.7       # was 0.5
    assert lt.convexity_watch_floor == 0.55     # was 0.6   (slight loosening for early WATCH)
    assert lt.pre_catalyst_event_min == 0.6     # was 0.75  (less relevant; Track B catalyst-independent)


# ---------------------------------------------------------------------------
# Override: risk_buckets — bucket-bazlı R reweighting
# ---------------------------------------------------------------------------


def test_risk_buckets_reweighted_for_track_b(gamma_squeeze_profile) -> None:  # type: ignore[no-untyped-def]
    """Track B raises CONVEXITY_CLUSTER (the prize signal), drops STANDARD_UOA
    (UOA-only is weak in Track B), drops LEAP_POSITIONING (Track B never
    holds LEAPs), drops PRE_CATALYST_FLOW (catalyst-independent thesis).
    """
    rb = gamma_squeeze_profile.risk_buckets
    assert rb.convexity_watch == 0.25              # unchanged
    assert rb.convexity_cluster == 1.00            # was 0.50  (CLUSTER+BURST = full)
    assert rb.standard_uoa == 0.25                 # was 0.75  (Track B weakens UOA-only)
    assert rb.sweep_uoa == 0.50                    # was 0.85
    assert rb.pre_catalyst_flow == 0.25            # was 0.85  (catalyst-independent)
    assert rb.high_conviction_sequence == 1.00     # unchanged (top of stack)
    assert rb.high_conviction_initial == 0.50      # unchanged
    assert rb.leap_positioning == 0.10             # was 0.25  (Track B doesn't trade LEAPs)
    assert rb.discard_or_log == 0.0                # unchanged
    assert rb.rejected == 0.0                      # unchanged


# ---------------------------------------------------------------------------
# Inherited (NOT overridden): must match v5_default exactly
# ---------------------------------------------------------------------------


def test_penalties_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Module 36 penalties stay Track-agnostic in this fork.
    If Track B-specific penalty tuning is wanted later, override here and
    update this test accordingly — but think carefully first; penalties
    encode universal trading risks (gap moves, IV exhaustion, thin OI)
    that don't change much by track.
    """
    assert gamma_squeeze_profile.penalties == default_profile.penalties


def test_penalty_triggers_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    assert gamma_squeeze_profile.penalty_triggers == default_profile.penalty_triggers


def test_fusion_block_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Fusion is Track-agnostic. confidence_tier filtering happens at runtime
    via CLI flag, not in the profile — that way the same profile drives all
    four cells of the combinatorial backtest (Tier-1+single, Tier-1+fusion,
    Tier-2+single, Tier-2+fusion).
    """
    assert gamma_squeeze_profile.fusion == default_profile.fusion


def test_time_of_day_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Time-of-day windows held at default until Tier-2 universe is selected;
    Tier-2 ticker time-of-day patterns may differ and warrant a separate fork.
    Decision deferred — see Phase 3 step 5 (universe selection).
    """
    assert gamma_squeeze_profile.time_of_day == default_profile.time_of_day


def test_relative_premium_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """The loader emits a 'profile_missing_commonly_tuned_field' warning for
    relative_premium.median_window_days. That's deliberate: median_window_days
    interacts with the MedianTradeSizeProvider implementation, which is a
    Phase 3 deliverable. Tuning this value is paired with the data work, not
    done blind here. When the provider lands, fork relative_premium and
    update this test.
    """
    assert gamma_squeeze_profile.relative_premium == default_profile.relative_premium


def test_contradiction_inherited_from_default(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    assert gamma_squeeze_profile.contradiction == default_profile.contradiction


def test_sub_score_missing_behavior_inherited(
    gamma_squeeze_profile, default_profile,  # type: ignore[no-untyped-def]
) -> None:
    """Missing-data policy is universal: 0.0 for missing sub-scores, raise
    on time_of_day_weight. No reason to differ by track.
    """
    assert (
        gamma_squeeze_profile.sub_score_missing_behavior
        == default_profile.sub_score_missing_behavior
    )
