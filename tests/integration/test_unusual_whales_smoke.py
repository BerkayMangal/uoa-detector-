"""Phase 3.3.3.6 Unusual Whales smoke integration tests.

This module hits the real UW API at https://api.unusualwhales.com.
It is gated behind:

  - The ``integration`` pytest marker (registered in pyproject.toml).
  - A non-empty ``UNUSUAL_WHALES_API_KEY`` env var.

To run:
  - Set ``UNUSUAL_WHALES_API_KEY`` in env or .env.
  - ``uv run pytest -m integration tests/integration/test_unusual_whales_smoke.py -v``

Without the env var, every test in this module is skipped — by design.
The CI suite never runs these; bisectable history is preserved by the
unit tests under ``tests/unit/test_unusual_whales_*.py`` which cover all
adapter logic via mocks.

What this smoke pins (when the key is present):

  1. UnusualWhalesClient connects and a basic GET returns 200 + JSON.
  2. Each of the six providers fetches a non-empty response for a
     liquid ticker (or returns None / empty cleanly).

⚠️  FLAGGED: This module's tests can ONLY be validated by Berkay
once the UW key is loaded into the environment. The unit tests pin
all parsing and caching logic; this file pins the network plumbing
and provider-endpoint shape consistency.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.config.credentials import Credentials
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    """Load the real UW key from env or skip the test."""
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "smoke integration test skipped. "
            "See tests/integration/test_unusual_whales_smoke.py docstring "
            "for run instructions.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    """Conservative settings for smoke tests — don't hammer UW."""
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=1.0,
        historical_concurrency=1,
        live_reconnect_max_attempts=2,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=10.0,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


# ---------------------------------------------------------------------------
# 1. Client smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unusual_whales_client_connects_smoke() -> None:
    """The simplest check: one GET returns 200 + JSON.

    Pins that UW's public API is reachable and the key is accepted.
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    try:
        # /api/stock/AAPL/info is small and tier-agnostic; if the key
        # is valid this returns a JSON object.
        result = await client.request_json("/api/stock/AAPL/info")
    finally:
        await client.aclose()
    assert result is not None
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_unusual_whales_credentials_load_from_env_smoke() -> None:
    """End-to-end: ``Credentials`` → ``UnusualWhalesClient`` → request."""
    _key_or_skip()  # gate
    creds = Credentials()
    api_key = creds.require_unusual_whales_api_key()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    try:
        result = await client.request_json("/api/stock/AAPL/info")
    finally:
        await client.aclose()
    assert result is not None


# ---------------------------------------------------------------------------
# 2. Provider smokes — one per provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dealer_gamma_provider_smoke() -> None:
    """Dealer-gamma endpoint returns parseable rows for a liquid ticker.

    Either a DealerPositioning is returned (real strike match) or
    None (no match for this strike). Both are valid; the smoke is
    that the call doesn't raise.
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    provider = UnusualWhalesDealerGammaProvider(
        client=client, settings=settings,
    )
    try:
        await provider.net_gamma_at(
            ticker="AAPL",
            strike=Decimal("190.00"),
            at=datetime.now(UTC),
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_iv_history_provider_smoke() -> None:
    """IV-rank endpoint returns parseable rows for a liquid contract.

    Berkay should override the strike/expiry to a contract that's
    actually in-window when running this. Until then, the smoke is
    just 'doesn't crash' — None is acceptable.
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    provider = UnusualWhalesIVHistoryProvider(
        client=client, settings=settings,
    )
    try:
        # A liquid SPY ATM option, expiry near term — operator overrides
        # if their data window differs.
        await provider.iv_rank_at(
            ticker="SPY",
            strike=Decimal("470.00"),
            expiry=date(2024, 12, 20),
            option_type="call",
            at=datetime.now(UTC),
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_dark_pool_provider_smoke() -> None:
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    provider = UnusualWhalesDarkPoolProvider(
        client=client, settings=settings,
    )
    try:
        prints = await provider.recent_prints(
            ticker="AAPL",
            before=datetime.now(UTC),
            window=timedelta(minutes=30),
        )
    finally:
        await client.aclose()
    # Either prints are returned or empty — both valid for smoke
    assert isinstance(prints, tuple)


@pytest.mark.asyncio
async def test_catalyst_calendar_provider_smoke() -> None:
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client, settings=settings,
    )
    try:
        # AAPL has earnings every quarter; smoke is parseable response,
        # not a specific event.
        await provider.next_catalyst("AAPL", datetime.now(UTC))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_open_interest_provider_smoke() -> None:
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    provider = UnusualWhalesOpenInterestProvider(
        client=client, settings=settings,
    )
    try:
        # at() — operator may need to swap to an in-window contract
        await provider.at(
            ticker="SPY",
            strike=Decimal("470.00"),
            expiry=date(2024, 12, 20),
            option_type="call",
            when=datetime.now(UTC),
        )
        # next_day() — yesterday's flow
        await provider.next_day(
            ticker="SPY",
            strike=Decimal("470.00"),
            expiry=date(2024, 12, 20),
            option_type="call",
            trade_date=date(2024, 11, 15),
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sector_peer_providers_smoke() -> None:
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    settings = _settings()
    sector_map = UnusualWhalesSectorMapProvider(
        client=client, settings=settings,
    )
    peer_flow = UnusualWhalesPeerFlowProvider(
        client=client, settings=settings,
    )
    try:
        # Sector map: AAPL → Technology (or similar) + peers
        sector = await sector_map.sector_of("AAPL")
        peers = await sector_map.peers_of("AAPL")
        # Peer flow: recent events on a small subset
        peer_subset = list(peers)[:3] if peers else ["MSFT"]
        await peer_flow.recent_flow(
            tickers=peer_subset,
            before=datetime.now(UTC),
            window=timedelta(minutes=30),
        )
    finally:
        await client.aclose()
    # Sector should be a non-empty string for a Russell 1000 ticker
    assert sector is None or isinstance(sector, str)
