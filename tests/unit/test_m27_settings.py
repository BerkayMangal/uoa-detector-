"""Phase 3.4.7.1 tests for ``scoring.modules.m27`` profile section.

Pins:
  - M27Settings defaults match acceptance doc:
      strong_opening_threshold = 0.5
      moderate_opening_threshold = 0.1
      closing_threshold = -0.1
      strong_opening_score = 1.0
      moderate_opening_score = 0.7
      neutral_score = 0.5
      closing_score = 0.0
      new_strike_score = 1.0
      timeout_score = 0.5
      no_data_score = 0.5
      session_close_utc_hour = 21
      session_close_utc_minute = 0
      provider_timeout_s = 2.0
      provider_cache_ttl_s = 300
  - extra=forbid (strict)
  - Bounds: thresholds ∈ valid ranges; scores ∈ [0, 1];
            session hour ∈ [0, 23]; session minute ∈ [0, 59]
  - closing_threshold MUST be negative (lt=0)
  - v5_default.yaml round-trip
  - profile.scoring.modules.m27 path resolves
  - M21..M26 still work
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M27Settings, ModulesSettings


def test_m27_defaults_match_acceptance_doc() -> None:
    s = M27Settings()
    assert s.strong_opening_threshold == 0.5
    assert s.moderate_opening_threshold == 0.1
    assert s.closing_threshold == -0.1
    assert s.strong_opening_score == 1.0
    assert s.moderate_opening_score == 0.7
    assert s.neutral_score == 0.5
    assert s.closing_score == 0.0
    assert s.new_strike_score == 1.0
    assert s.timeout_score == 0.5
    assert s.no_data_score == 0.5
    assert s.session_close_utc_hour == 21
    assert s.session_close_utc_minute == 0
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 300


def test_m27_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M27Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_strong_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M27Settings(strong_opening_threshold=0.0)


def test_strong_threshold_max_10() -> None:
    M27Settings(strong_opening_threshold=10.0)
    with pytest.raises(ValueError):
        M27Settings(strong_opening_threshold=10.01)


def test_moderate_threshold_max_one() -> None:
    M27Settings(moderate_opening_threshold=1.0)
    with pytest.raises(ValueError):
        M27Settings(moderate_opening_threshold=1.01)


def test_closing_threshold_must_be_negative() -> None:
    """Acceptance doc invariant: closing means OI DROPPED → negative."""
    with pytest.raises(ValueError):
        M27Settings(closing_threshold=0.0)
    with pytest.raises(ValueError):
        M27Settings(closing_threshold=0.1)


def test_closing_threshold_min_minus_one() -> None:
    """OI can't decrease by more than 100% (-100% = everyone closed)."""
    M27Settings(closing_threshold=-1.0)
    with pytest.raises(ValueError):
        M27Settings(closing_threshold=-1.01)


def test_score_fields_bounded_zero_one() -> None:
    M27Settings(strong_opening_score=0.0)
    M27Settings(closing_score=1.0)
    with pytest.raises(ValueError):
        M27Settings(neutral_score=1.01)
    with pytest.raises(ValueError):
        M27Settings(timeout_score=-0.01)


def test_session_hour_bounds() -> None:
    M27Settings(session_close_utc_hour=0)
    M27Settings(session_close_utc_hour=23)
    with pytest.raises(ValueError):
        M27Settings(session_close_utc_hour=24)
    with pytest.raises(ValueError):
        M27Settings(session_close_utc_hour=-1)


def test_session_minute_bounds() -> None:
    M27Settings(session_close_utc_minute=0)
    M27Settings(session_close_utc_minute=59)
    with pytest.raises(ValueError):
        M27Settings(session_close_utc_minute=60)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M27Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    M27Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m27_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m27, M27Settings)
    # M21..M26 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m25.peer_count == 5
    assert m.m26.dark_pool_lookback_minutes == 60


def test_v5_default_loads_m27_block() -> None:
    profile = load_default_profile()
    m27 = profile.scoring.modules.m27
    assert m27.strong_opening_threshold == 0.5
    assert m27.moderate_opening_threshold == 0.1
    assert m27.closing_threshold == -0.1
    assert m27.new_strike_score == 1.0
    assert m27.session_close_utc_hour == 21


def test_profile_path_scoring_modules_m27_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m27.strong_opening_threshold == 0.5


def test_operator_can_override_thresholds() -> None:
    s = M27Settings(
        strong_opening_threshold=0.3,
        moderate_opening_threshold=0.05,
        closing_threshold=-0.2,
    )
    assert s.strong_opening_threshold == 0.3
    assert s.moderate_opening_threshold == 0.05
    assert s.closing_threshold == -0.2
