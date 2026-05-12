"""Phase 3.4.3.4 M23 integration smoke test.

Connects PriceConfirmationStage to the real ThetaData
PriceActionProvider for SPY and asserts the resulting
price_confirmation_score is in [0.0, 1.0] with a recognised
branch label.

GATED BY:
  - ``THETADATA_API_KEY`` env var present
    (different from M21/M22 smokes — this is the first M-module
    that uses ThetaData rather than Unusual Whales)
  - ``@pytest.mark.integration`` marker
  - Theta Terminal v3 must be running locally at
    http://127.0.0.1:25503 (Phase 3.3.7 migrated from v2 port
    25510 to v3 port 25503; see docs/DATA_INTEGRATION.md)

Skipped without the key. CI never runs this; Berkay runs manually
after loading credentials. The unit tests
(``tests/unit/test_m23_stage.py`` + ``tests/unit/test_price_movement.py``)
pin all branches + telemetry pathways via mocks; this smoke
confirms wiring against the live ThetaData endpoint.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.sources.thetadata.client import ThetaDataClient
from uoa_detector.sources.thetadata.providers.price_action import (
    ThetaDataPriceActionProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("THETADATA_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "THETADATA_API_KEY not set in environment; "
            "M23 integration smoke skipped. "
            "Theta Terminal must also be running locally. "
            "See docs/DATA_INTEGRATION.md for setup instructions.",
        )
    return SecretStr(raw)


def _settings() -> ThetaDataSettings:
    """Conservative settings for the smoke."""
    return ThetaDataSettings(
        rate_limit_requests_per_second=2.0,
        historical_concurrency=1,
        live_reconnect_max_attempts=2,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=10.0,
    )


def _spy_event(option_type: str = "call") -> EnrichedEvent:
    """Build a minimal SPY event for the smoke.

    Timestamp = 1 hour ago (so the lookback window has data even
    if Theta Terminal is starting fresh and hasn't fully indexed
    today's intraday yet).
    """
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    op = OptionsPrint(
        event_id="m23-smoke-1",
        timestamp=one_hour_ago,
        ticker="SPY",
        option_type=option_type,  # type: ignore[arg-type]
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
            sources_seen=("thetadata",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


@pytest.mark.asyncio
async def test_m23_smoke_against_real_thetadata_for_spy() -> None:
    """Wire PriceConfirmationStage to real ThetaData provider; SPY end-to-end.

    Asserts:
      - The stage's enrich() doesn't raise
      - price_confirmation_score is in [0.0, 1.0]
      - last_execution_metadata is populated with a recognised branch
      - When Theta Terminal returns spot data: branch is one of
        call_confirmed/call_contrarian/neutral
      - When data is unavailable: branch is data_missing_neutral
    """
    api_key = _key_or_skip()
    client = ThetaDataClient(api_key=api_key, settings=_settings())
    provider = ThetaDataPriceActionProvider(
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
    }
