"""Phase 3.3.2.1 tests for ``ThetaDataSettings`` + ``DataSourcesConfig``.

Pins:
  - default values track the Pro plan + Phase 3 prep
  - bounds: rate_limit > 0, concurrency >= 1, reconnect_max_attempts >= 0
  - profile loads explicit overrides from yaml without losing defaults
  - omitting data_sources from a profile still produces valid defaults
    (default_factory pattern)
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    DataSourcesConfig,
    ThetaDataSettings,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_thetadata_defaults_match_phase_3_prep() -> None:
    """The Pro plan defaults are pinned. Any change must be deliberate."""
    s = ThetaDataSettings()
    assert s.rate_limit_requests_per_second == 10.0
    assert s.historical_concurrency == 4
    assert s.live_reconnect_max_attempts == 5
    assert s.live_reconnect_initial_backoff_s == 1.0
    assert s.live_reconnect_max_backoff_s == 60.0


def test_v5_default_carries_explicit_data_sources_block() -> None:
    """profiles/v5_default.yaml has the explicit data_sources block —
    operator-visible defaults rather than implicit ones.

    Phase 3.5.3.3/3.5.3.5: values raised from the conservative
    10 req/s / 4 concurrent to the PRO-tier-observed 25 req/s /
    8 concurrent (Terminal reports 'Max concurrent requests: 8').
    """
    profile = load_default_profile()
    assert profile.data_sources.thetadata.rate_limit_requests_per_second == 25.0
    assert profile.data_sources.thetadata.historical_concurrency == 8


# ---------------------------------------------------------------------------
# Bounds validation
# ---------------------------------------------------------------------------


def test_rate_limit_must_be_positive() -> None:
    with pytest.raises(Exception, match="rate_limit_requests_per_second"):
        ThetaDataSettings(rate_limit_requests_per_second=0.0)
    with pytest.raises(Exception, match="rate_limit_requests_per_second"):
        ThetaDataSettings(rate_limit_requests_per_second=-1.0)


def test_rate_limit_capped() -> None:
    with pytest.raises(Exception, match="rate_limit_requests_per_second"):
        ThetaDataSettings(rate_limit_requests_per_second=10000.0)


def test_concurrency_must_be_at_least_one() -> None:
    with pytest.raises(Exception, match="historical_concurrency"):
        ThetaDataSettings(historical_concurrency=0)


def test_concurrency_capped() -> None:
    """Don't let an operator set concurrency=1000 by accident."""
    with pytest.raises(Exception, match="historical_concurrency"):
        ThetaDataSettings(historical_concurrency=64)


def test_reconnect_max_attempts_can_be_zero() -> None:
    """0 disables reconnect entirely — useful for tests that want to
    assert a single-failure path. Pinned by docstring on the field."""
    s = ThetaDataSettings(live_reconnect_max_attempts=0)
    assert s.live_reconnect_max_attempts == 0


def test_reconnect_max_attempts_negative_rejected() -> None:
    with pytest.raises(Exception, match="live_reconnect_max_attempts"):
        ThetaDataSettings(live_reconnect_max_attempts=-1)


def test_reconnect_backoff_must_be_positive() -> None:
    with pytest.raises(Exception, match="initial_backoff"):
        ThetaDataSettings(live_reconnect_initial_backoff_s=0.0)
    with pytest.raises(Exception, match="max_backoff"):
        ThetaDataSettings(live_reconnect_max_backoff_s=0.0)


# ---------------------------------------------------------------------------
# DataSourcesConfig defaulting
# ---------------------------------------------------------------------------


def test_data_sources_defaults_to_thetadata_defaults() -> None:
    """An empty DataSourcesConfig() produces a fully-populated
    ThetaDataSettings under .thetadata. default_factory pattern
    means yaml omitting data_sources still produces valid defaults."""
    cfg = DataSourcesConfig()
    assert cfg.thetadata.rate_limit_requests_per_second == 10.0


def test_calibration_profile_has_data_sources_field(
    request: pytest.FixtureRequest,
) -> None:
    """The CalibrationProfile carries a data_sources field with
    the expected nested shape."""
    profile = load_default_profile()
    assert isinstance(profile.data_sources, DataSourcesConfig)
    assert isinstance(profile.data_sources.thetadata, ThetaDataSettings)


# ---------------------------------------------------------------------------
# YAML override surface
# ---------------------------------------------------------------------------


def test_profile_can_override_thetadata_concurrency() -> None:
    """Constructing a profile with custom data_sources overrides
    the inherited defaults for the named field only."""
    base = load_default_profile()
    overridden = base.model_copy(
        update={
            "data_sources": DataSourcesConfig(
                thetadata=ThetaDataSettings(
                    historical_concurrency=2,
                ),
            ),
        },
    )
    assert overridden.data_sources.thetadata.historical_concurrency == 2
    # Other fields default unchanged
    assert overridden.data_sources.thetadata.rate_limit_requests_per_second == 10.0
