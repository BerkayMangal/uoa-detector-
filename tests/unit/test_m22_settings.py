"""Phase 3.4.2.1 tests for ``scoring.modules.m22`` profile section.

Pins:
  - M22Settings defaults match acceptance doc:
      post_event_blackout_days = 1
      pre_event_window_days = 14
      no_catalyst_neutral_score = 0.3
      dte_survives_score = 1.0
      dte_expires_before_score = 0.5
      post_event_score = 0.0
      provider_timeout_s = 2.0
      provider_cache_ttl_s = 3600
  - extra=forbid (strict)
  - Bounds:
      post_event_blackout_days ∈ [0, 30]
      pre_event_window_days ∈ [1, 90]
      score fields ∈ [0, 1]
  - v5_default.yaml carries the m22 block
  - profile.scoring.modules.m22 path resolves
  - Backwards-compat: old YAML without m22 still loads
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M22Settings, ModulesSettings


def test_m22_defaults_match_acceptance_doc() -> None:
    s = M22Settings()
    assert s.post_event_blackout_days == 1
    assert s.pre_event_window_days == 14
    assert s.no_catalyst_neutral_score == 0.3
    assert s.dte_survives_score == 1.0
    assert s.dte_expires_before_score == 0.5
    assert s.post_event_score == 0.0
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 3600


def test_m22_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M22Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_post_event_blackout_zero_allowed() -> None:
    """blackout=0 means 'only same-day catalysts count' — valid."""
    s = M22Settings(post_event_blackout_days=0)
    assert s.post_event_blackout_days == 0


def test_post_event_blackout_max_30() -> None:
    M22Settings(post_event_blackout_days=30)
    with pytest.raises(ValueError):
        M22Settings(post_event_blackout_days=31)


def test_post_event_blackout_negative_rejected() -> None:
    with pytest.raises(ValueError):
        M22Settings(post_event_blackout_days=-1)


def test_pre_event_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M22Settings(pre_event_window_days=0)


def test_pre_event_window_max_90() -> None:
    M22Settings(pre_event_window_days=90)
    with pytest.raises(ValueError):
        M22Settings(pre_event_window_days=91)


def test_score_fields_bounded_zero_one() -> None:
    M22Settings(no_catalyst_neutral_score=0.0)
    M22Settings(no_catalyst_neutral_score=1.0)
    with pytest.raises(ValueError):
        M22Settings(no_catalyst_neutral_score=1.01)
    with pytest.raises(ValueError):
        M22Settings(dte_survives_score=-0.01)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M22Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    s = M22Settings(provider_cache_ttl_s=0)
    assert s.provider_cache_ttl_s == 0


def test_modules_settings_carries_m22_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m22, M22Settings)
    # M21 still present
    assert m.m21.flip_proximity_pct == 0.03


def test_v5_default_loads_m22_block() -> None:
    profile = load_default_profile()
    m22 = profile.scoring.modules.m22
    assert m22.post_event_blackout_days == 1
    assert m22.pre_event_window_days == 14
    assert m22.no_catalyst_neutral_score == 0.3
    assert m22.dte_survives_score == 1.0
    assert m22.dte_expires_before_score == 0.5
    assert m22.post_event_score == 0.0


def test_profile_path_scoring_modules_m22_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m22.pre_event_window_days == 14


def test_operator_can_override_window_days() -> None:
    """Operator profile may pin tighter or looser windows."""
    s = M22Settings(
        post_event_blackout_days=3,
        pre_event_window_days=7,
    )
    assert s.post_event_blackout_days == 3
    assert s.pre_event_window_days == 7
