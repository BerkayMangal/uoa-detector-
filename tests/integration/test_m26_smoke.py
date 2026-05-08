"""Phase 3.4.6.3 M26 integration smoke test.

Connects DarkPoolStage to the real UW UnusualWhalesDarkPoolProvider
for SPY and asserts the resulting state is shape-valid.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - ``@pytest.mark.integration`` marker

Skipped without the key. The unit tests (``tests/unit/test_m26_*``)
pin all branches + telemetry pathways via mocks; this smoke
confirms wiring against the live UW endpoint.
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
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M26 integration smoke skipped. "
            "Run with the key in .env or export the var.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _spy_event() -> EnrichedEvent:
    """Build a minimal SPY event for the smoke.

    SPY has heavy DP activity; reliable target for confirming the
    multi-print fetch + filter pipeline works.
    """
    op = OptionsPrint(
        event_id="m26-smoke-1",
        timestamp=datetime.now(UTC),
        ticker="SPY",
        option_type="call",
        strike=Decimal("500.00"),
        expiry=date(2099, 12, 31),
        dte=10000,
        spot_price=Decimal("500.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.20,
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
async def test_m26_smoke_against_real_uw_for_spy() -> None:
    """Wire DarkPoolStage to real UW provider; SPY end-to-end.

    Asserts:
      - stage.enrich() doesn't raise
      - last_execution_metadata is populated with a recognised branch
      - has_dark_pool_confirmation is True iff branch == confirmed_match
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    provider = UnusualWhalesDarkPoolProvider(
        client=client, settings=_settings(),
    )
    stage = DarkPoolStage(provider=provider)

    try:
        event = _spy_event()
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    assert stage.last_execution_metadata is not None
    branch = stage.last_execution_metadata["branch"]
    assert branch in {
        "confirmed_match",
        "direction_unclear",
        "direction_mismatch",
        "no_qualifying_prints",
        "unknown_option_type",
        "timeout",
    }
    # Flag set iff confirmed
    if branch == "confirmed_match":
        assert event.has_dark_pool_confirmation is True
    else:
        assert event.has_dark_pool_confirmation is False
