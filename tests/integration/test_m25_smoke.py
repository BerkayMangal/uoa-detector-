"""Phase 3.4.5.3 M25 integration smoke test.

Connects SectorPeerStage to BOTH real UW providers
(UnusualWhalesSectorMapProvider + UnusualWhalesPeerFlowProvider)
and asserts the resulting state is shape-valid.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - ``@pytest.mark.integration`` marker

Skipped without the key. The unit tests
(``tests/unit/test_m25_stage.py`` + ``tests/unit/test_m25_settings.py``)
pin all branches + telemetry pathways via mocks; this smoke
confirms wiring against the live UW endpoints.

Note: M25 is the first M-module that fans out across multiple
peer tickers in one HTTP call. Smoke uses AAPL (Technology
sector — reliably has > 5 peers + active flow during RTH).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M25 integration smoke skipped. "
            "Run with the key in .env or export the var.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _aapl_event() -> EnrichedEvent:
    """Build a minimal AAPL event for the smoke.

    AAPL is in Technology, which has many peers (MSFT, GOOGL, META,
    NVDA, etc.) — reliable target for the multi-ticker peer flow
    fetch.
    """
    op = OptionsPrint(
        event_id="m25-smoke-1",
        timestamp=datetime.now(UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("180.00"),
        expiry=date(2099, 12, 31),
        dte=10000,
        spot_price=Decimal("180.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.25,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=10000,
        source_agreement=SourceAgreement(
            sources_seen=("unusual_whales",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


@pytest.mark.asyncio
async def test_m25_smoke_against_real_uw_for_aapl() -> None:
    """Wire SectorPeerStage to real UW providers; AAPL end-to-end.

    Asserts:
      - stage.enrich() doesn't raise
      - sector_confirmation_score is in [0.0, 1.0]
      - last_execution_metadata is populated with a recognised branch
      - When real peers + flow are returned: branch reflects alignment
      - When data unavailable / timeout: branch is no_sector or timeout
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    sector_provider = UnusualWhalesSectorMapProvider(
        client=client, settings=_settings(),
    )
    peer_flow_provider = UnusualWhalesPeerFlowProvider(
        client=client, settings=_settings(),
    )
    stage = SectorPeerStage(
        sector_provider=sector_provider,
        peer_flow_provider=peer_flow_provider,
    )

    try:
        event = _aapl_event()
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    assert event.sector_confirmation_score is not None
    assert 0.0 <= event.sector_confirmation_score <= 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] in {
        "strong",
        "moderate",
        "weak",
        "contrarian",
        "empty_peer_flow",
        "all_neutral",
        "no_sector",
        "timeout",
        "unknown_option_type",
    }
