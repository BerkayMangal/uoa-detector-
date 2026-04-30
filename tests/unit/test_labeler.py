"""Unit tests for the 17-label decision tree.

At least one positive case per label, plus precedence checks for the
short-circuit gates (LEAP > POST_EVENT > PENALIZED > others).
"""

from __future__ import annotations

from tests.conftest import build_enriched, build_print
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import AppliedPenalty
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.labeling.labeler import Labeler


def _label(event) -> SignalLabel:  # type: ignore[no-untyped-def]
    """Run the labeler with default config and return the chosen label."""
    return Labeler(load_default_profile()).decide(event).label


def _score(event, pre: float, post: float) -> None:  # type: ignore[no-untyped-def]
    """Stamp pre/post scores on an event so the labeler can run."""
    event.combined_score_pre_penalty = pre
    event.combined_score_post_penalty = post


# --- Short-circuit gates (precedence) ----------------------------------------


def test_leap_beats_everything_including_post_event_and_penalized() -> None:
    pr = build_print(dte=180)
    event = build_enriched(print_=pr)
    event.is_post_event = True  # would otherwise trigger POST_EVENT_NOISE
    event.applied_penalties.append(
        AppliedPenalty(name="thin_oi", value=-0.20, reason="x")
    )
    _score(event, pre=-0.5, post=-0.5)  # would also trigger PENALIZED
    assert _label(event) == SignalLabel.LEAP_POSITIONING


def test_post_event_beats_penalized_and_below() -> None:
    event = build_enriched()
    event.is_post_event = True
    event.applied_penalties.append(
        AppliedPenalty(name="thin_oi", value=-0.20, reason="x")
    )
    _score(event, pre=0.4, post=0.1)  # would otherwise trigger PENALIZED
    assert _label(event) == SignalLabel.POST_EVENT_NOISE


def test_penalized_beats_oi_failure_and_below() -> None:
    event = build_enriched()
    event.next_day_oi_confirmed = False
    _score(event, pre=0.4, post=0.20)  # below 0.30 threshold
    assert _label(event) == SignalLabel.PENALIZED_BELOW_THRESHOLD


def test_oi_failure_downgrades_to_likely_closing() -> None:
    event = build_enriched()
    event.next_day_oi_confirmed = False
    _score(event, pre=0.7, post=0.6)  # above threshold so PENALIZED won't fire
    assert _label(event) == SignalLabel.LIKELY_CLOSING_OR_NOISE


# --- Tradeable / informational labels ----------------------------------------


def test_high_conviction_sequence_requires_all_three() -> None:
    event = build_enriched(cluster=0.6, sweep="iso")
    event.has_price_confirmation = True
    _score(event, pre=0.8, post=0.8)
    assert _label(event) == SignalLabel.HIGH_CONVICTION_SEQUENCE


def test_high_conviction_falls_back_without_price_confirmation() -> None:
    """Cluster + sweep but no price confirmation → SWEEP_UOA, not HCS."""
    event = build_enriched(cluster=0.6, sweep="iso")
    event.has_price_confirmation = False
    _score(event, pre=0.7, post=0.7)
    assert _label(event) == SignalLabel.SWEEP_UOA


def test_confirmed_opening_flow_when_oi_explicitly_true() -> None:
    event = build_enriched(cluster=0.0, sweep="block")
    event.next_day_oi_confirmed = True
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.CONFIRMED_OPENING_FLOW


def test_pre_catalyst_flow_when_cluster_plus_imminent_event() -> None:
    event = build_enriched(cluster=0.5, event_score=0.85, sweep="block")
    event.has_price_confirmation = False  # no HCS
    _score(event, pre=0.7, post=0.7)
    assert _label(event) == SignalLabel.PRE_CATALYST_FLOW


def test_sweep_uoa_on_iso_classification() -> None:
    pr = build_print(is_iso=True, fill_side="above_ask")
    event = build_enriched(print_=pr, sweep="iso", cluster=0.0)
    _score(event, pre=0.7, post=0.7)
    assert _label(event) == SignalLabel.SWEEP_UOA


def test_standard_uoa_when_above_ask_and_relative_premium_high() -> None:
    pr = build_print(fill_side="above_ask")
    event = build_enriched(
        print_=pr, sweep="block", relative_premium=0.6, cluster=0.0,
    )
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.STANDARD_UOA


def test_options_equity_tape_confirmation() -> None:
    pr = build_print(fill_side="midpoint")  # avoid STANDARD_UOA
    event = build_enriched(print_=pr, sweep="block", cluster=0.0, relative_premium=0.3)
    event.has_dark_pool_confirmation = True
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.OPTIONS_EQUITY_TAPE_CONFIRMATION


def test_sector_flow_cluster() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(print_=pr, sweep="block", cluster=0.0, relative_premium=0.3)
    event.has_sector_confirmation = True
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.SECTOR_FLOW_CLUSTER


def test_gamma_acceleration_risk() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(print_=pr, sweep="block", cluster=0.0, relative_premium=0.3)
    event.is_gamma_acceleration = True
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.GAMMA_ACCELERATION_RISK


def test_convexity_burst_at_0_8() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(print_=pr, sweep="block", cluster=0.85, relative_premium=0.3)
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.CONVEXITY_BURST


def test_convexity_cluster_at_0_5() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(print_=pr, sweep="block", cluster=0.5, relative_premium=0.3)
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.CONVEXITY_CLUSTER


def test_opening_unconfirmed_with_aggressive_fill_pending_oi() -> None:
    pr = build_print(fill_side="above_ask")
    event = build_enriched(
        print_=pr, sweep="block", cluster=0.0,
        relative_premium=0.2,  # below STANDARD_UOA threshold
        convexity=0.3,         # below CONVEXITY_WATCH floor (0.6)
    )
    event.next_day_oi_confirmed = None
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.OPENING_UNCONFIRMED


def test_convexity_watch_with_high_convexity_no_cluster() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(
        print_=pr, sweep="block", cluster=0.0, convexity=0.7,
        relative_premium=0.3,
    )
    _score(event, pre=0.6, post=0.6)
    assert _label(event) == SignalLabel.CONVEXITY_WATCH


def test_ignore_noise_default() -> None:
    pr = build_print(fill_side="midpoint")
    event = build_enriched(
        print_=pr, sweep="block", cluster=0.0,
        convexity=0.3, relative_premium=0.3,
    )
    _score(event, pre=0.5, post=0.5)
    assert _label(event) == SignalLabel.IGNORE_NOISE


# --- Coverage of all 17 labels ----------------------------------------------


def test_all_seventeen_labels_have_a_test_above() -> None:
    """Sanity check: the SignalLabel enum has exactly 17 entries."""
    assert len(list(SignalLabel)) == 17
