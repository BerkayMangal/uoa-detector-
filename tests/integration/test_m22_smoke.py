"""Phase 3.4.2.4 M22 integration smoke test.

Connects EventCalendarStage to the real UW
CatalystCalendarProvider for SPY and asserts the resulting
event_score is in [0.0, 1.0] with a recognised branch label.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - ``@pytest.mark.integration`` marker

Skipped without the key. CI never runs this; Berkay runs it
manually after loading the key. The unit tests
(``tests/unit/test_m22_stage.py`` + ``tests/unit/test_catalyst_window.py``)
pin all branches + telemetry pathways via mocks; this smoke just
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
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M22 integration smoke skipped. "
            "Run with the key in .env or export the var to exercise.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _spy_event() -> EnrichedEvent:
    """Build a minimal SPY event for the smoke."""
    op = OptionsPrint(
        event_id="smoke-1",
        timestamp=datetime.now(UTC),
        ticker="SPY",
        option_type="call",
        strike=Decimal("500.00"),
        expiry=date(2099, 12, 31),  # far-future placeholder so DTE > any catalyst
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
async def test_m22_smoke_against_real_uw_for_spy() -> None:
    """Wire EventCalendarStage to real UW CatalystCalendarProvider; SPY end-to-end.

    Asserts:
      - The stage's enrich() doesn't raise
      - event_score is a float in [0.0, 1.0]
      - last_execution_metadata is populated with a recognised branch
      - No timeout (UW responds within profile.modules.m22.provider_timeout_s)
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client, settings=_settings(),
    )
    stage = EventCalendarStage(provider=provider)

    try:
        event = _spy_event()
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    assert event.event_score is not None
    assert 0.0 <= event.event_score <= 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] in {
        "no_catalyst",
        "post_event_blackout",
        "pre_event_dte_survives",
        "pre_event_dte_expires_before",
        "neutral_fallback",
    }
    # Provider returned either yes or empty (not 'no' which is the timeout label)
    assert stage.last_execution_metadata["provider_returned"] in {"yes", "empty"}
