"""Unit tests for ``build_live_stage_pipeline`` — Phase 3.8.

Asserts the live builder wires the REAL Unusual Whales providers into each
M-stage (not the NoOp defaults), preserves the canonical stage order from
``default_stage_pipeline``, and shares one client across every provider.

No network: the ``UnusualWhalesClient`` is constructed with a dummy key and
default settings — construction only spins up an in-memory httpx client and
token bucket; no request is made.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.pipeline.stages import (
    build_live_stage_pipeline,
    default_stage_pipeline,
)
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
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
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)


@pytest.fixture
def client() -> UnusualWhalesClient:
    return UnusualWhalesClient(
        api_key=SecretStr("test-key-not-used"),
        settings=UnusualWhalesSettings(),
    )


def test_same_stage_order_and_types_as_default(
    client: UnusualWhalesClient,
) -> None:
    """The live pipeline is the default pipeline with providers wired.

    Same length, same stage class per position.
    """
    profile = load_default_profile()
    default = default_stage_pipeline()
    live = build_live_stage_pipeline(client, profile)

    assert len(live) == len(default)
    assert [type(s).__name__ for s in live] == [
        type(s).__name__ for s in default
    ]


def test_every_m_stage_carries_its_real_uw_provider(
    client: UnusualWhalesClient,
) -> None:
    """Each M-stage holds the concrete UW provider, never a NoOp."""
    profile = load_default_profile()
    stages = build_live_stage_pipeline(client, profile)
    by_type = {type(s): s for s in stages}

    m21 = by_type[DealerGammaStage]
    assert isinstance(m21._provider, UnusualWhalesDealerGammaProvider)

    m22 = by_type[EventCalendarStage]
    assert isinstance(m22._provider, UnusualWhalesCatalystCalendarProvider)

    m23 = by_type[PriceConfirmationStage]
    assert isinstance(m23._provider, UnusualWhalesPriceActionProvider)

    m24 = by_type[IVExhaustionStage]
    assert isinstance(m24._iv_provider, UnusualWhalesIVHistoryProvider)
    assert isinstance(
        m24._catalyst_provider, UnusualWhalesCatalystCalendarProvider,
    )

    m25 = by_type[SectorPeerStage]
    assert isinstance(m25._sector_provider, UnusualWhalesSectorMapProvider)
    assert isinstance(m25._peer_flow_provider, UnusualWhalesPeerFlowProvider)

    m26 = by_type[DarkPoolStage]
    assert isinstance(m26._provider, UnusualWhalesDarkPoolProvider)

    m27 = by_type[OpeningClosingStage]
    assert isinstance(m27._provider, UnusualWhalesOpenInterestProvider)


def test_default_pipeline_still_uses_noop_providers() -> None:
    """Guard: the offline default must NOT reach out to a vendor.

    A regression here would mean the offline/backtest paths started
    hitting UW — the opposite of the bug we are fixing.
    """
    stages = {type(s): s for s in default_stage_pipeline()}

    m21 = stages[DealerGammaStage]
    assert not isinstance(m21._provider, UnusualWhalesDealerGammaProvider)
    m27 = stages[OpeningClosingStage]
    assert not isinstance(m27._provider, UnusualWhalesOpenInterestProvider)


def test_providers_share_the_one_injected_client(
    client: UnusualWhalesClient,
) -> None:
    """All providers must be backed by the SAME client instance."""
    profile = load_default_profile()
    stages = {type(s): s for s in build_live_stage_pipeline(client, profile)}

    assert stages[DealerGammaStage]._provider._client is client
    assert stages[EventCalendarStage]._provider._client is client
    assert stages[PriceConfirmationStage]._provider._client is client
    assert stages[IVExhaustionStage]._iv_provider._client is client
    assert stages[IVExhaustionStage]._catalyst_provider._client is client
    assert stages[SectorPeerStage]._sector_provider._client is client
    assert stages[SectorPeerStage]._peer_flow_provider._client is client
    assert stages[DarkPoolStage]._provider._client is client
    assert stages[OpeningClosingStage]._provider._client is client
