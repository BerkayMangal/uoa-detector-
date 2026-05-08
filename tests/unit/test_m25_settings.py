"""Phase 3.4.5.1 tests for ``scoring.modules.m25`` profile section.

Pins:
  - M25Settings defaults match acceptance doc
  - extra=forbid (strict)
  - Bounds: peer_window ∈ [1, 240]; peer_count ∈ [1, 50];
            thresholds ∈ (0, 1]; scores ∈ [0, 1]
  - provider_timeout_s default 3.0 (HIGHER than other modules
    per acceptance doc — multi-ticker fetch)
  - v5_default.yaml round-trip
  - profile.scoring.modules.m25 path resolves
  - M21..M24 still work (back-compat)
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M25Settings, ModulesSettings


def test_m25_defaults_match_acceptance_doc() -> None:
    s = M25Settings()
    assert s.peer_window_minutes == 30
    assert s.peer_count == 5
    assert s.strong_alignment_threshold == 0.6
    assert s.moderate_alignment_threshold == 0.4
    assert s.weak_alignment_threshold == 0.2
    assert s.strong_alignment_score == 1.0
    assert s.moderate_alignment_score == 0.7
    assert s.weak_alignment_score == 0.3
    assert s.contrarian_score == 0.0
    assert s.no_sector_score == 0.5
    assert s.empty_peer_flow_score == 0.5
    assert s.timeout_score == 0.5
    assert s.provider_timeout_s == 3.0  # HIGHER
    assert s.provider_cache_ttl_s == 60


def test_m25_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M25Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_peer_window_bounds() -> None:
    M25Settings(peer_window_minutes=1)
    M25Settings(peer_window_minutes=240)
    with pytest.raises(ValueError):
        M25Settings(peer_window_minutes=0)
    with pytest.raises(ValueError):
        M25Settings(peer_window_minutes=241)


def test_peer_count_bounds() -> None:
    M25Settings(peer_count=1)
    M25Settings(peer_count=50)
    with pytest.raises(ValueError):
        M25Settings(peer_count=0)
    with pytest.raises(ValueError):
        M25Settings(peer_count=51)


def test_alignment_thresholds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M25Settings(strong_alignment_threshold=0.0)
    with pytest.raises(ValueError):
        M25Settings(moderate_alignment_threshold=0.0)
    # weak_alignment_threshold allows 0 (boundary case)
    M25Settings(weak_alignment_threshold=0.0)


def test_alignment_thresholds_max_one() -> None:
    M25Settings(strong_alignment_threshold=1.0)
    with pytest.raises(ValueError):
        M25Settings(strong_alignment_threshold=1.01)


def test_score_fields_bounded_zero_one() -> None:
    M25Settings(strong_alignment_score=0.0)
    M25Settings(strong_alignment_score=1.0)
    with pytest.raises(ValueError):
        M25Settings(contrarian_score=1.01)
    with pytest.raises(ValueError):
        M25Settings(timeout_score=-0.01)


def test_provider_timeout_higher_than_other_modules() -> None:
    """M25's default 3.0s is intentionally above M21-M24's 2.0s.

    Acceptance doc directive: 'multi-ticker fetch needs longer
    budget'.
    """
    s = M25Settings()
    assert s.provider_timeout_s == 3.0


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M25Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    M25Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m25_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m25, M25Settings)
    # M21..M24 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m22.pre_event_window_days == 14
    assert m.m23.lookback_minutes == 30
    assert m.m24.high_iv_threshold == 80.0


def test_v5_default_loads_m25_block() -> None:
    profile = load_default_profile()
    m25 = profile.scoring.modules.m25
    assert m25.peer_count == 5
    assert m25.peer_window_minutes == 30
    assert m25.strong_alignment_threshold == 0.6
    assert m25.contrarian_score == 0.0
    assert m25.provider_timeout_s == 3.0


def test_profile_path_scoring_modules_m25_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m25.peer_count == 5


def test_operator_can_override_peer_count_and_window() -> None:
    """Operator may pin wider peer set or longer window."""
    s = M25Settings(peer_count=20, peer_window_minutes=120)
    assert s.peer_count == 20
    assert s.peer_window_minutes == 120


def test_operator_can_override_thresholds_independently() -> None:
    """Trigger thresholds can shift without changing score values."""
    s = M25Settings(
        strong_alignment_threshold=0.75,
        weak_alignment_threshold=0.10,
    )
    assert s.strong_alignment_threshold == 0.75
    assert s.weak_alignment_threshold == 0.10
