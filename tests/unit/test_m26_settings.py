"""Phase 3.4.6.1 tests for ``scoring.modules.m26`` profile section.

Pins:
  - M26Settings defaults match acceptance doc:
      dark_pool_lookback_minutes = 60
      min_print_size_usd = 5_000_000
      confirmed_match_score = 1.0
      direction_unclear_score = 0.5
      no_qualifying_prints_score = 0.2
      timeout_score = 0.5
      delay_warning_minutes = 5
      provider_timeout_s = 2.0
      provider_cache_ttl_s = 30
  - extra=forbid (strict)
  - Bounds: lookback ∈ [1, 480]; min_size >= 0;
            score fields ∈ [0, 1]; delay_warning ∈ [0, 60]
  - v5_default.yaml round-trip
  - profile.scoring.modules.m26 path resolves
  - M21..M25 still work
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M26Settings, ModulesSettings


def test_m26_defaults_match_acceptance_doc() -> None:
    s = M26Settings()
    assert s.dark_pool_lookback_minutes == 60
    assert s.min_print_size_usd == 5_000_000
    assert s.confirmed_match_score == 1.0
    assert s.direction_unclear_score == 0.5
    assert s.no_qualifying_prints_score == 0.2
    assert s.timeout_score == 0.5
    assert s.delay_warning_minutes == 5
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 30


def test_m26_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M26Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_lookback_bounds() -> None:
    M26Settings(dark_pool_lookback_minutes=1)
    M26Settings(dark_pool_lookback_minutes=480)
    with pytest.raises(ValueError):
        M26Settings(dark_pool_lookback_minutes=0)
    with pytest.raises(ValueError):
        M26Settings(dark_pool_lookback_minutes=481)


def test_min_print_size_zero_allowed() -> None:
    """Operator may set zero to disable size filter for testing."""
    s = M26Settings(min_print_size_usd=0)
    assert s.min_print_size_usd == 0


def test_min_print_size_negative_rejected() -> None:
    with pytest.raises(ValueError):
        M26Settings(min_print_size_usd=-1)


def test_score_fields_bounded_zero_one() -> None:
    M26Settings(confirmed_match_score=0.0)
    M26Settings(confirmed_match_score=1.0)
    with pytest.raises(ValueError):
        M26Settings(direction_unclear_score=1.01)
    with pytest.raises(ValueError):
        M26Settings(no_qualifying_prints_score=-0.01)


def test_delay_warning_bounds() -> None:
    M26Settings(delay_warning_minutes=0)
    M26Settings(delay_warning_minutes=60)
    with pytest.raises(ValueError):
        M26Settings(delay_warning_minutes=-1)
    with pytest.raises(ValueError):
        M26Settings(delay_warning_minutes=61)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M26Settings(provider_timeout_s=0.0)


def test_provider_cache_ttl_zero_allowed() -> None:
    M26Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m26_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m26, M26Settings)
    # M21..M25 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m25.peer_count == 5


def test_v5_default_loads_m26_block() -> None:
    profile = load_default_profile()
    m26 = profile.scoring.modules.m26
    assert m26.dark_pool_lookback_minutes == 60
    assert m26.min_print_size_usd == 5_000_000
    assert m26.no_qualifying_prints_score == 0.2
    assert m26.delay_warning_minutes == 5


def test_profile_path_scoring_modules_m26_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m26.dark_pool_lookback_minutes == 60


def test_operator_can_override_size_threshold() -> None:
    """Small-cap operator may lower threshold to $1M."""
    s = M26Settings(min_print_size_usd=1_000_000)
    assert s.min_print_size_usd == 1_000_000


def test_no_qualifying_default_is_low_not_zero() -> None:
    """Acceptance doc invariant: 0.2 not 0.0 — sparse-data fallback."""
    s = M26Settings()
    assert 0.0 < s.no_qualifying_prints_score < 0.5
    assert s.no_qualifying_prints_score == 0.2
