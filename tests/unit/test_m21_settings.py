"""Phase 3.4.1.1 tests for ``scoring.modules.m21`` profile section.

Pins:
  - M21Settings defaults match acceptance doc:
      short_gamma_threshold = -50_000_000
      flip_proximity_pct    = 0.03
      extreme_distance_pct  = 0.20
      provider_timeout_s    = 2.0
      provider_cache_ttl_s  = 300
  - extra=forbid (strict)
  - Bounds: flip_proximity_pct, extreme_distance_pct ∈ (0, 1]
  - Bounds: provider_timeout_s ∈ (0, 60]
  - Bounds: provider_cache_ttl_s >= 0
  - short_gamma_threshold accepts arbitrary signed Decimal
  - Profile YAML round-trip: v5_default.yaml carries the m21 block
  - Backwards-compat: profile YAML WITHOUT scoring.modules still
    loads (default_factory provides M21Settings)
  - CalibrationProfile.scoring.modules.m21 path resolves
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    M21Settings,
    ModulesSettings,
    ScoringWeights,
)

# ---------------------------------------------------------------------------
# Defaults match acceptance doc
# ---------------------------------------------------------------------------


def test_m21_defaults_match_acceptance_doc() -> None:
    s = M21Settings()
    assert s.short_gamma_threshold == Decimal("-50_000_000")
    assert s.flip_proximity_pct == 0.03
    assert s.extreme_distance_pct == 0.20
    assert s.provider_timeout_s == 2.0
    assert s.provider_cache_ttl_s == 300


def test_m21_extra_forbid() -> None:
    with pytest.raises(ValueError):
        M21Settings(unknown_field=1.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_flip_proximity_pct_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M21Settings(flip_proximity_pct=0.0)


def test_flip_proximity_pct_max_one() -> None:
    s = M21Settings(flip_proximity_pct=1.0)
    assert s.flip_proximity_pct == 1.0
    with pytest.raises(ValueError):
        M21Settings(flip_proximity_pct=1.01)


def test_extreme_distance_pct_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M21Settings(extreme_distance_pct=0.0)


def test_extreme_distance_pct_max_one() -> None:
    s = M21Settings(extreme_distance_pct=1.0)
    assert s.extreme_distance_pct == 1.0


def test_provider_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        M21Settings(provider_timeout_s=0.0)


def test_provider_timeout_max_60s() -> None:
    s = M21Settings(provider_timeout_s=60.0)
    assert s.provider_timeout_s == 60.0
    with pytest.raises(ValueError):
        M21Settings(provider_timeout_s=60.01)


def test_provider_cache_ttl_zero_allowed() -> None:
    """ttl=0 means 'do not cache'; that's a valid operator choice."""
    s = M21Settings(provider_cache_ttl_s=0)
    assert s.provider_cache_ttl_s == 0


def test_provider_cache_ttl_negative_rejected() -> None:
    with pytest.raises(ValueError):
        M21Settings(provider_cache_ttl_s=-1)


# ---------------------------------------------------------------------------
# short_gamma_threshold accepts arbitrary signed Decimal
# ---------------------------------------------------------------------------


def test_short_gamma_threshold_accepts_zero() -> None:
    s = M21Settings(short_gamma_threshold=Decimal("0"))
    assert s.short_gamma_threshold == Decimal("0")


def test_short_gamma_threshold_accepts_positive() -> None:
    """Positive thresholds are unusual but legal — operator may pin
    a 'never qualify as short' value e.g. for the always-long-gamma
    side of a comparison run."""
    s = M21Settings(short_gamma_threshold=Decimal("100_000_000"))
    assert s.short_gamma_threshold == Decimal("100_000_000")


def test_short_gamma_threshold_accepts_int_from_yaml() -> None:
    """YAML may load -50_000_000 as int; Pydantic coerces to Decimal."""
    s = M21Settings(short_gamma_threshold=-25_000_000)  # type: ignore[arg-type]
    assert s.short_gamma_threshold == Decimal("-25_000_000")


# ---------------------------------------------------------------------------
# ModulesSettings + ScoringWeights wiring
# ---------------------------------------------------------------------------


def test_modules_settings_default_factory_creates_m21() -> None:
    m = ModulesSettings()
    assert isinstance(m.m21, M21Settings)


def test_scoring_weights_carries_modules_default() -> None:
    s = ScoringWeights(
        uoa=0.30, convexity=0.25, event=0.15, gamma=0.10,
        price_confirmation=0.10, sector_confirmation=0.05, time_of_day=0.05,
    )
    assert isinstance(s.modules, ModulesSettings)
    assert s.modules.m21.flip_proximity_pct == 0.03


# ---------------------------------------------------------------------------
# v5_default.yaml round-trip
# ---------------------------------------------------------------------------


def test_v5_default_loads_m21_block() -> None:
    """The explicit YAML block matches the M21Settings defaults."""
    profile = load_default_profile()
    m21 = profile.scoring.modules.m21
    assert m21.short_gamma_threshold == Decimal("-50_000_000")
    assert m21.flip_proximity_pct == 0.03
    assert m21.extreme_distance_pct == 0.20
    assert m21.provider_timeout_s == 2.0
    assert m21.provider_cache_ttl_s == 300


def test_profile_path_scoring_modules_m21_resolves() -> None:
    """The 'scoring.modules.m21' path the acceptance doc references
    is a real Python attribute chain on CalibrationProfile."""
    profile = load_default_profile()
    # If any of these AttributeErrors out, the path is broken.
    assert profile.scoring.modules.m21.flip_proximity_pct == 0.03
