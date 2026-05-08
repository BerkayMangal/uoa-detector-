"""Phase 3.4.4.4 M24 integration smoke test.

Connects IVExhaustionStage to the real UW IVHistoryProvider +
CatalystCalendarProvider for SPY and asserts the resulting state
is shape-valid.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present (M24 uses UW for
    BOTH providers — IV history and catalyst calendar)
  - ``@pytest.mark.integration`` marker

Skipped without the key. The unit tests
(``tests/unit/test_m24_stage.py`` + ``tests/unit/test_m24_settings.py``
+ ``tests/unit/test_combined_score_pre_adjustment.py``) pin all
branches + telemetry pathways via mocks; this smoke confirms
wiring against the live UW endpoints.

Note: M24 uses TWO providers. Smoke exercises both providers
through one stage call.
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
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M24 integration smoke skipped. "
            "Run with the key in .env or export the var.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _spy_event() -> EnrichedEvent:
    """Build a minimal SPY event for the smoke."""
    op = OptionsPrint(
        event_id="m24-smoke-1",
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
async def test_m24_smoke_against_real_uw_for_spy() -> None:
    """Wire IVExhaustionStage to real UW providers; SPY end-to-end.

    Asserts:
      - stage.enrich() doesn't raise
      - last_execution_metadata is populated with a recognised branch
      - if a penalty was emitted, ScoreAdjustment shape is valid
        (target=combined_score_pre, delta < 0, source_module=m24)
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    iv_provider = UnusualWhalesIVHistoryProvider(
        client=client, settings=_settings(),
    )
    catalyst_provider = UnusualWhalesCatalystCalendarProvider(
        client=client, settings=_settings(),
    )
    stage = IVExhaustionStage(
        iv_provider=iv_provider,
        catalyst_provider=catalyst_provider,
    )

    try:
        event = _spy_event()
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] in {
        "cheap",
        "moderate",
        "elevated",
        "expensive",
        "no_iv_history",
        "iv_provider_timeout",
    }
    # If a penalty was emitted, validate its shape
    if stage.last_execution_metadata.get(
        "post_earnings_penalty_emitted",
    ) == "yes":
        assert len(event.score_adjustments) == 1
        adj = event.score_adjustments[0]
        assert adj.target == "combined_score_pre"
        assert adj.delta < 0
        assert adj.source_module == "m24_iv_exhaustion"
