"""Phase 3.4.8.1a tests for ``scoring.modules.m28`` profile section.

Pins:
  - M28Settings defaults match acceptance doc:
      min_m27_score_to_validate = 0.7
      confirmed_score = 1.0
      ambiguous_score = 0.5
      closing_score = 0.0
      provider_timeout_s = 5.0  (HIGHER than other modules)
      batch_run_time_et = "09:31"
      provider_cache_ttl_s = 86400
  - extra=forbid (strict)
  - batch_run_time_et validation: HH:MM 24-hour only
  - Score fields ∈ [0, 1]; min_m27 ∈ [0, 1]
  - provider_timeout_s up to 120 (batch tolerates slow)
  - v5_default.yaml round-trip
  - profile.scoring.modules.m28 path resolves
  - M21..M27 still work
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M28Settings, ModulesSettings


def test_m28_defaults_match_acceptance_doc() -> None:
    s = M28Settings()
    assert s.min_m27_score_to_validate == 0.7
    assert s.confirmed_score == 1.0
    assert s.ambiguous_score == 0.5
    assert s.closing_score == 0.0
    assert s.provider_timeout_s == 5.0
    assert s.batch_run_time_et == "09:31"
    assert s.provider_cache_ttl_s == 86400


def test_m28_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M28Settings(unknown_field=1.0)  # type: ignore[call-arg]


def test_min_m27_score_bounds() -> None:
    M28Settings(min_m27_score_to_validate=0.0)
    M28Settings(min_m27_score_to_validate=1.0)
    with pytest.raises(ValueError):
        M28Settings(min_m27_score_to_validate=-0.1)
    with pytest.raises(ValueError):
        M28Settings(min_m27_score_to_validate=1.1)


def test_score_fields_bounded_zero_one() -> None:
    M28Settings(confirmed_score=0.0)
    M28Settings(confirmed_score=1.0)
    with pytest.raises(ValueError):
        M28Settings(ambiguous_score=1.01)
    with pytest.raises(ValueError):
        M28Settings(closing_score=-0.01)


def test_provider_timeout_higher_than_other_modules() -> None:
    """5.0s default reflects batch-job tolerance for slow endpoints."""
    s = M28Settings()
    assert s.provider_timeout_s == 5.0


def test_provider_timeout_max_120s() -> None:
    """Allow up to 2 minutes for very slow batch operations."""
    M28Settings(provider_timeout_s=120.0)
    with pytest.raises(ValueError):
        M28Settings(provider_timeout_s=120.1)


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M28Settings(provider_timeout_s=0.0)


def test_batch_run_time_valid_formats() -> None:
    """All valid HH:MM 24-hour strings accepted."""
    for t in ["00:00", "09:31", "16:00", "23:59", "12:30"]:
        s = M28Settings(batch_run_time_et=t)
        assert s.batch_run_time_et == t


def test_batch_run_time_invalid_format_rejected() -> None:
    """Non-HH:MM strings rejected."""
    invalid = ["", "9:31", "09:60", "24:00", "9pm", "9:31am", "0931", "09:1"]
    for t in invalid:
        with pytest.raises(ValueError, match="batch_run_time_et"):
            M28Settings(batch_run_time_et=t)


def test_provider_cache_ttl_zero_allowed() -> None:
    M28Settings(provider_cache_ttl_s=0)


def test_modules_settings_carries_m28_default() -> None:
    m = ModulesSettings()
    assert isinstance(m.m28, M28Settings)
    # M21..M27 still present
    assert m.m21.flip_proximity_pct == 0.03
    assert m.m27.strong_opening_threshold == 0.5


def test_v5_default_loads_m28_block() -> None:
    profile = load_default_profile()
    m28 = profile.scoring.modules.m28
    assert m28.min_m27_score_to_validate == 0.7
    assert m28.confirmed_score == 1.0
    assert m28.batch_run_time_et == "09:31"


def test_profile_path_scoring_modules_m28_resolves() -> None:
    profile = load_default_profile()
    assert profile.scoring.modules.m28.batch_run_time_et == "09:31"


def test_operator_can_lower_m27_threshold_for_full_validation() -> None:
    """Operator may set 0.0 to validate every signal at higher cost."""
    s = M28Settings(min_m27_score_to_validate=0.0)
    assert s.min_m27_score_to_validate == 0.0
