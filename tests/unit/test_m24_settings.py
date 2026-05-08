"""Phase 3.4.4.1 tests for ``scoring.modules.m24`` profile section.

Pins:
  - M24Settings defaults match acceptance doc:
      low_iv_threshold = 30.0
      mid_iv_threshold = 60.0
      high_iv_threshold = 80.0
      cheap_iv_score = 1.0
      moderate_iv_score = 0.7
      elevated_iv_score = 0.3
      expensive_iv_score = 0.0
      no_iv_history_score = 0.5
      post_earnings_iv_penalty = -0.4
      post_earnings_iv_penalty_threshold = 80.0
      post_earnings_session_days = 1
      provider_timeout_s = 2.0
      provider_cache_ttl_s = 600
  - extra=forbid (strict)
  - Bounds: thresholds ∈ [0, 100]; scores ∈ [0, 1];
            penalty <= 0; session_days ∈ [0, 10]
  - v5_default.yaml carries the m24 block
  - profile.scoring.modules.m24 path resolves
  - Backwards-compat: M21+M22+M23 still work
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M24Settings, ModulesSettings


def test_m24_defaults_match_acceptance_doc() -> None:
    s = M24Settings()
    assert s.low_iv_threshold == 30.0
    assert s.mid_iv_threshold == 60.0
    assert s.high_iv_threshold == 80.0
    assert s.cheap_iv_score == 1.0
    assert s.moderate_iv_score == 0.7
    assert s.elevated_iv_score == 0.3
    assert s.expensive_iv_score == 0.0
    assert s.no_iv_history_score == 0.5
    assert s.post_earnings_iv_penalty == -0.4
    assert s.post_earnings_iv_penalty_threshold == 80.0
    assert s.post_earnings_session_days == 1
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 600


def test_m24_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M24Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_thresholds_bounded_zero_hundred() -> None:
    M24Settings(low_iv_threshold=0.0)
    M24Settings(high_iv_threshold=100.0)
    with pytest.raises(ValueError):
        M24Settings(low_iv_threshold=-0.1)
    with pytest.raises(ValueError):
        M24Settings(high_iv_threshold=100.1)


def test_score_fields_bounded_zero_one() -> None:
    M24Settings(cheap_iv_score=0.0)
    M24Settings(cheap_iv_score=1.0)
    with pytest.raises(ValueError):
        M24Settings(moderate_iv_score=1.01)
    with pytest.raises(ValueError):
        M24Settings(elevated_iv_score=-0.01)


def test_post_earnings_penalty_must_be_non_positive() -> None:
    """Penalty is a deduction (negative or zero)."""
    M24Settings(post_earnings_iv_penalty=0.0)
    M24Settings(post_earnings_iv_penalty=-1.0)
    with pytest.raises(ValueError):
        M24Settings(post_earnings_iv_penalty=0.01)


def test_session_days_bounded() -> None:
    M24Settings(post_earnings_session_days=0)
    M24Settings(post_earnings_session_days=10)
    with pytest.raises(ValueError):
        M24Settings(post_earnings_session_days=-1)
    with pytest.raises(ValueError):
        M24Settings(post_earnings_session_days=11)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M24Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    M24Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m24_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m24, M24Settings)
    # M21..M23 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m22.pre_event_window_days == 14
    assert m.m23.lookback_minutes == 30


def test_v5_default_loads_m24_block() -> None:
    profile = load_default_profile()
    m24 = profile.scoring.modules.m24
    assert m24.low_iv_threshold == 30.0
    assert m24.mid_iv_threshold == 60.0
    assert m24.high_iv_threshold == 80.0
    assert m24.post_earnings_iv_penalty == -0.4
    assert m24.post_earnings_iv_penalty_threshold == 80.0
    assert m24.post_earnings_session_days == 1


def test_profile_path_scoring_modules_m24_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m24.high_iv_threshold == 80.0


def test_operator_can_override_thresholds() -> None:
    """Operator may pin tighter or wider IV bands for comparison runs."""
    s = M24Settings(
        low_iv_threshold=20.0,
        mid_iv_threshold=50.0,
        high_iv_threshold=70.0,
    )
    assert s.low_iv_threshold == 20.0
    assert s.high_iv_threshold == 70.0


def test_operator_can_override_penalty_independently() -> None:
    """Penalty value separable from trigger threshold."""
    s = M24Settings(
        post_earnings_iv_penalty=-0.2,
        post_earnings_iv_penalty_threshold=75.0,
    )
    assert s.post_earnings_iv_penalty == -0.2
    assert s.post_earnings_iv_penalty_threshold == 75.0
