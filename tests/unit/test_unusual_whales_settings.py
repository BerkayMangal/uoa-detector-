"""Phase 3.3.3.1 tests for ``UnusualWhalesSettings`` + cache TTL block.

Pins:
  - default values track the API-Plus tier + Phase 3 prep
  - bounds: rate_limit > 0, concurrency >= 1, reconnect_max_attempts >= 0
  - cache TTL defaults match Phase 3.3 acceptance doc
    (calendar 3600, gamma 300, IV 600, sector 86400, DP 60, OI 600)
  - profile loads explicit unusual_whales block from yaml
  - omitting data_sources.unusual_whales from a profile still produces
    valid defaults (default_factory pattern)
  - DataSourcesConfig now exposes both thetadata and unusual_whales
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    DataSourcesConfig,
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_unusual_whales_defaults_match_phase_3_prep() -> None:
    """API-Plus tier defaults are pinned. Changes must be deliberate."""
    s = UnusualWhalesSettings()
    assert s.rate_limit_requests_per_second == 2.0
    assert s.historical_concurrency == 2
    assert s.live_reconnect_max_attempts == 5
    assert s.live_reconnect_initial_backoff_s == 1.0
    assert s.live_reconnect_max_backoff_s == 60.0


def test_unusual_whales_cache_ttl_defaults() -> None:
    """Phase 3.3 acceptance: 'cache_ttl_seconds per provider type'."""
    ttl = UnusualWhalesProviderCacheTTL()
    assert ttl.catalyst_calendar_seconds == 3600
    assert ttl.dealer_gamma_seconds == 300
    assert ttl.iv_history_seconds == 600
    assert ttl.sector_map_seconds == 86_400
    assert ttl.dark_pool_seconds == 60
    assert ttl.open_interest_seconds == 600


def test_unusual_whales_settings_includes_cache_ttl_default() -> None:
    """``UnusualWhalesSettings.cache_ttl`` defaults via default_factory."""
    s = UnusualWhalesSettings()
    assert isinstance(s.cache_ttl, UnusualWhalesProviderCacheTTL)
    assert s.cache_ttl.catalyst_calendar_seconds == 3600


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_rate_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        UnusualWhalesSettings(rate_limit_requests_per_second=0.0)


def test_historical_concurrency_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        UnusualWhalesSettings(historical_concurrency=0)


def test_reconnect_max_attempts_can_be_zero() -> None:
    """0 disables reconnect — useful for tests asserting single failure."""
    s = UnusualWhalesSettings(live_reconnect_max_attempts=0)
    assert s.live_reconnect_max_attempts == 0


def test_reconnect_initial_backoff_must_be_positive() -> None:
    with pytest.raises(ValueError):
        UnusualWhalesSettings(live_reconnect_initial_backoff_s=0.0)


def test_cache_ttl_seconds_can_be_zero_to_disable() -> None:
    """0 disables caching for that provider — every request hits the API."""
    ttl = UnusualWhalesProviderCacheTTL(catalyst_calendar_seconds=0)
    assert ttl.catalyst_calendar_seconds == 0


def test_cache_ttl_sector_map_can_be_one_week() -> None:
    """sector_map_seconds bound is 1 week (86400 * 7) — sector mapping is glacial."""
    ttl = UnusualWhalesProviderCacheTTL(sector_map_seconds=604_800)
    assert ttl.sector_map_seconds == 604_800
    with pytest.raises(ValueError):
        UnusualWhalesProviderCacheTTL(sector_map_seconds=604_801)


# ---------------------------------------------------------------------------
# DataSourcesConfig composition
# ---------------------------------------------------------------------------


def test_data_sources_config_now_has_unusual_whales_field() -> None:
    """DataSourcesConfig exposes both thetadata + unusual_whales."""
    cfg = DataSourcesConfig()
    assert hasattr(cfg, "thetadata")
    assert hasattr(cfg, "unusual_whales")
    assert isinstance(cfg.unusual_whales, UnusualWhalesSettings)


def test_data_sources_config_strict_extra_forbid() -> None:
    """Unknown keys at the data_sources level must raise."""
    with pytest.raises(ValueError):
        DataSourcesConfig(unknown_source={})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Profile YAML round-trip
# ---------------------------------------------------------------------------


def test_v5_default_carries_explicit_unusual_whales_block() -> None:
    """v5_default.yaml has the explicit unusual_whales block."""
    profile = load_default_profile()
    uw = profile.data_sources.unusual_whales
    assert uw.rate_limit_requests_per_second == 1.5  # 90/min, headroom under 120/min
    assert uw.historical_concurrency == 2
    assert uw.live_reconnect_max_attempts == 5
    assert uw.cache_ttl.catalyst_calendar_seconds == 3600
    assert uw.cache_ttl.dealer_gamma_seconds == 300
    assert uw.cache_ttl.iv_history_seconds == 600
    assert uw.cache_ttl.sector_map_seconds == 86_400
    assert uw.cache_ttl.dark_pool_seconds == 60
    assert uw.cache_ttl.open_interest_seconds == 600
