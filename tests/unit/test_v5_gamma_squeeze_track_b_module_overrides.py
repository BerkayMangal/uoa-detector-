"""Phase 3.4.9.2 tests for v5_gamma_squeeze module overrides.

Pins:
  - v5_gamma_squeeze loads with the new modules: block
  - M21 overrides: flip_proximity_pct=0.025, extreme_distance_pct=0.15
  - M27 overrides: moderate_opening_threshold=0.05
  - Non-overridden M21 fields inherited from default (e.g.,
    short_gamma_threshold)
  - M22-M26 + M28 entire blocks inherited unchanged
  - Modules sum to 8 (M21..M28) under scoring.modules in Track B
  - Combined-score weights still sum to 1.0
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    CalibrationProfile,
)

_PROFILES_DIR = Path(__file__).parent.parent.parent / "profiles"


@pytest.fixture(scope="module")
def gamma_squeeze_profile() -> CalibrationProfile:
    """Load v5_gamma_squeeze profile (deep-merged from v5_default)."""
    from uoa_detector.calibration.loader import load_profile
    return load_profile(
        _PROFILES_DIR / "v5_gamma_squeeze.yaml",
        profiles_dir=_PROFILES_DIR,
    )


def test_track_b_m21_flip_proximity_tightened(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Phase 3.4.9.2: Track B tightens flip_proximity for earlier signal."""
    assert gamma_squeeze_profile.scoring.modules.m21.flip_proximity_pct == 0.025
    # Default would be 0.03
    default = load_default_profile()
    assert default.scoring.modules.m21.flip_proximity_pct == 0.03


def test_track_b_m21_extreme_distance_tightened(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Phase 3.4.9.2: Track B collapses sooner."""
    assert (
        gamma_squeeze_profile.scoring.modules.m21.extreme_distance_pct == 0.15
    )
    default = load_default_profile()
    assert default.scoring.modules.m21.extreme_distance_pct == 0.20


def test_track_b_m21_short_gamma_threshold_inherited(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Track B does NOT override short_gamma_threshold (market-mechanics number)."""
    default = load_default_profile()
    assert (
        gamma_squeeze_profile.scoring.modules.m21.short_gamma_threshold
        == default.scoring.modules.m21.short_gamma_threshold
    )
    # And value is the v5_default constant
    assert (
        gamma_squeeze_profile.scoring.modules.m21.short_gamma_threshold
        == -50_000_000
    )


def test_track_b_m27_moderate_threshold_lowered(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Phase 3.4.9.2: Track B catches stealthy accumulation (lower threshold)."""
    assert (
        gamma_squeeze_profile.scoring.modules.m27.moderate_opening_threshold
        == 0.05
    )
    default = load_default_profile()
    assert default.scoring.modules.m27.moderate_opening_threshold == 0.10


def test_track_b_m27_strong_threshold_inherited(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Strong opening still requires clear OI growth — not overridden."""
    default = load_default_profile()
    assert (
        gamma_squeeze_profile.scoring.modules.m27.strong_opening_threshold
        == default.scoring.modules.m27.strong_opening_threshold
    )
    assert (
        gamma_squeeze_profile.scoring.modules.m27.strong_opening_threshold
        == 0.5
    )


def test_track_b_m22_to_m26_m28_inherited_unchanged(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """All non-M21/M27 module blocks inherited from v5_default unchanged."""
    default = load_default_profile()
    track_b = gamma_squeeze_profile.scoring.modules
    # Each block compared by its full model_dump()
    assert track_b.m22.model_dump() == default.scoring.modules.m22.model_dump()
    assert track_b.m23.model_dump() == default.scoring.modules.m23.model_dump()
    assert track_b.m24.model_dump() == default.scoring.modules.m24.model_dump()
    assert track_b.m25.model_dump() == default.scoring.modules.m25.model_dump()
    assert track_b.m26.model_dump() == default.scoring.modules.m26.model_dump()
    assert track_b.m28.model_dump() == default.scoring.modules.m28.model_dump()


def test_track_b_combined_score_weights_still_sum_to_one(
    gamma_squeeze_profile: CalibrationProfile,
) -> None:
    """Phase 3.4.9.2 yaml edits do NOT alter the combined-score weight sum."""
    weights = gamma_squeeze_profile.scoring
    total = (
        weights.uoa + weights.convexity + weights.event + weights.gamma
        + weights.price_confirmation + weights.sector_confirmation
        + weights.time_of_day
    )
    assert abs(total - 1.0) < 1e-9


def test_track_b_module_overrides_yaml_in_correct_block() -> None:
    """The yaml file structurally has modules: under scoring: (no duplicates)."""
    parser = YAML(typ="safe")
    raw = parser.load((_PROFILES_DIR / "v5_gamma_squeeze.yaml").read_text())
    assert "scoring" in raw
    assert "modules" in raw["scoring"]
    assert "m21" in raw["scoring"]["modules"]
    assert "m27" in raw["scoring"]["modules"]
    # M21/M27 are the ONLY overridden modules
    assert set(raw["scoring"]["modules"].keys()) == {"m21", "m27"}
