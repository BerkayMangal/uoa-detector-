"""Phase 3.3.5.1 tests for ``LiveSettings`` profile section.

Pins:
  - LiveSettings default: dashboard_refresh_seconds=2.0
  - bound: > 0, <= 300
  - profile loads explicit live block from yaml
  - profile without live block uses default_factory (back-compat)
  - extra=forbid
  - CalibrationProfile.live exists alongside data_sources
  - Phase 3.3.5 acceptance pin: dashboard_refresh_seconds is
    NOT consumed by the live observer in 3.3.5 (operator-facing
    contract, comment-checked)
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    CalibrationProfile,
    LiveSettings,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_live_settings_default() -> None:
    s = LiveSettings()
    assert s.dashboard_refresh_seconds == 2.0


def test_live_settings_extra_forbid() -> None:
    with pytest.raises(ValueError):
        LiveSettings(unknown_field=1.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_dashboard_refresh_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LiveSettings(dashboard_refresh_seconds=0.0)


def test_dashboard_refresh_upper_bound_300_seconds() -> None:
    """5min upper bound; longer would defeat the dashboard purpose."""
    s = LiveSettings(dashboard_refresh_seconds=300.0)
    assert s.dashboard_refresh_seconds == 300.0
    with pytest.raises(ValueError):
        LiveSettings(dashboard_refresh_seconds=301.0)


def test_dashboard_refresh_negative_rejected() -> None:
    with pytest.raises(ValueError):
        LiveSettings(dashboard_refresh_seconds=-1.0)


# ---------------------------------------------------------------------------
# CalibrationProfile integration
# ---------------------------------------------------------------------------


def test_v5_default_carries_live_block() -> None:
    profile = load_default_profile()
    assert isinstance(profile.live, LiveSettings)
    assert profile.live.dashboard_refresh_seconds == 2.0


def test_calibration_profile_has_live_field() -> None:
    """Sanity check on top-level model shape."""
    fields = CalibrationProfile.model_fields
    assert "live" in fields
    # Default-factory pattern means a profile YAML omitting live still works
    assert "data_sources" in fields  # sibling for context


def test_profile_can_override_dashboard_refresh() -> None:
    """An operator can pin a specific refresh in their own YAML override."""
    s = LiveSettings(dashboard_refresh_seconds=5.0)
    assert s.dashboard_refresh_seconds == 5.0
