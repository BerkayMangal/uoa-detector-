"""Phase 3.3.8.3 M23 integration smoke test.

Connects PriceConfirmationStage to the real UW
PriceActionProvider for SPY and asserts the resulting
price_confirmation_score is in [0.0, 1.0] with a recognised
branch label.

History:
  Phase 3.4.3.4 introduced this smoke against
  ``ThetaDataPriceActionProvider`` (the original M23 backend).
  Phase 3.3.8.3 swapped the backend to UW because ThetaData's
  stock OHLC endpoint requires a STOCK.VALUE subscription add-on
  that operators on OPTION.STANDARD don't have, and UW's
  /api/stock/{ticker}/ohlc/1m endpoint serves the same data
  under the API-Plus subscription that the other M-modules
  already use. See ``docs/phase-3.3.8-acceptance.md``.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - ``@pytest.mark.integration`` marker

Skipped without the key. CI never runs this; Berkay runs manually
after loading credentials. The unit tests
(``tests/unit/test_m23_stage.py`` +
``tests/unit/test_price_movement.py`` +
``tests/unit/test_unusual_whales_price_action.py``) pin all
branches + telemetry pathways via mocks; this smoke confirms
wiring against the live UW endpoint.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M23 integration smoke skipped. "
            "Run with the key in .env or export the var to exercise.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _spy_event(option_type: str = "call") -> EnrichedEvent:
    """Build a minimal SPY event for the smoke.

    Timestamp = 1 hour ago (so the lookback window has data even
    if the current minute's bar hasn't been published yet).
    """
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    op = OptionsPrint(
        event_id="m23-smoke-1",
        timestamp=one_hour_ago,
        ticker="SPY",
        option_type=option_type,
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
async def test_m23_smoke_against_real_uw_for_spy() -> None:
    """Wire PriceConfirmationStage to real UW provider; SPY end-to-end.

    Asserts:
      - The stage's enrich() doesn't raise
      - price_confirmation_score is in [0.0, 1.0]
      - last_execution_metadata is populated with a recognised branch
      - When UW returns spot data: branch is one of
        call_confirmed/call_contrarian/neutral
      - When data is unavailable: branch is data_missing_neutral
      - On transient UW HTTP error (rare; smoke tolerates it): branch
        is provider_error (Phase 3.3.8.1 graceful degradation)
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    provider = UnusualWhalesPriceActionProvider(
        client=client, settings=_settings(),
    )
    stage = PriceConfirmationStage(provider=provider)

    try:
        event = _spy_event(option_type="call")
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    assert event.price_confirmation_score is not None
    assert 0.0 <= event.price_confirmation_score <= 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] in {
        "call_confirmed",
        "call_contrarian",
        "neutral",
        "data_missing_neutral",
        "provider_error",
    }
