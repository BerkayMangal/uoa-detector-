"""Phase 3.4.3.1 tests for ``scoring.modules.m23`` profile section.

Pins:
  - M23Settings defaults match acceptance doc:
      lookback_minutes = 30
      confirmation_pct = 0.005
      confirmed_score = 1.0
      contrarian_score = 0.3
      neutral_score = 0.5
      session_open_utc_hour = 14
      session_open_utc_minute = 30
      provider_timeout_s = 2.0
      provider_cache_ttl_s = 60
  - extra=forbid (strict)
  - Bounds: lookback ∈ [1, 240]; confirmation_pct ∈ (0, 0.5];
            session hour ∈ [0, 23]; session minute ∈ [0, 59];
            score fields ∈ [0, 1]
  - v5_default.yaml carries the m23 block
  - profile.scoring.modules.m23 path resolves
  - Backwards-compat: M21+M22 still work
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M23Settings, ModulesSettings


def test_m23_defaults_match_acceptance_doc() -> None:
    s = M23Settings()
    assert s.lookback_minutes == 30
    assert s.confirmation_pct == 0.005
    assert s.confirmed_score == 1.0
    assert s.contrarian_score == 0.3
    assert s.neutral_score == 0.5
    assert s.session_open_utc_hour == 14
    assert s.session_open_utc_minute == 30
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 60


def test_m23_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M23Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_lookback_minutes_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M23Settings(lookback_minutes=0)


def test_lookback_minutes_max_240() -> None:
    M23Settings(lookback_minutes=240)
    with pytest.raises(ValueError):
        M23Settings(lookback_minutes=241)


def test_confirmation_pct_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M23Settings(confirmation_pct=0.0)


def test_confirmation_pct_max_50pct() -> None:
    M23Settings(confirmation_pct=0.5)
    with pytest.raises(ValueError):
        M23Settings(confirmation_pct=0.51)


def test_score_fields_bounded_zero_one() -> None:
    M23Settings(confirmed_score=0.0)
    M23Settings(neutral_score=1.0)
    with pytest.raises(ValueError):
        M23Settings(contrarian_score=1.01)
    with pytest.raises(ValueError):
        M23Settings(neutral_score=-0.01)


def test_session_hour_bounds() -> None:
    M23Settings(session_open_utc_hour=0)
    M23Settings(session_open_utc_hour=23)
    with pytest.raises(ValueError):
        M23Settings(session_open_utc_hour=24)
    with pytest.raises(ValueError):
        M23Settings(session_open_utc_hour=-1)


def test_session_minute_bounds() -> None:
    M23Settings(session_open_utc_minute=0)
    M23Settings(session_open_utc_minute=59)
    with pytest.raises(ValueError):
        M23Settings(session_open_utc_minute=60)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M23Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    M23Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m23_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m23, M23Settings)
    # M21 + M22 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m22.pre_event_window_days == 14


def test_v5_default_loads_m23_block() -> None:
    profile = load_default_profile()
    m23 = profile.scoring.modules.m23
    assert m23.lookback_minutes == 30
    assert m23.confirmation_pct == 0.005
    assert m23.confirmed_score == 1.0
    assert m23.contrarian_score == 0.3
    assert m23.neutral_score == 0.5


def test_profile_path_scoring_modules_m23_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m23.lookback_minutes == 30


def test_operator_can_override_lookback() -> None:
    """Operator profile may pin tighter or wider lookback."""
    s = M23Settings(lookback_minutes=15, confirmation_pct=0.002)
    assert s.lookback_minutes == 15
    assert s.confirmation_pct == 0.002
