"""Phase 3.4.1.4 M21 integration smoke test.

Connects DealerGammaStage to the real UW DealerGammaProvider for SPY
and asserts the resulting gamma_score is in [0.0, 1.0] (or None if
UW returns no data — both outcomes are valid; the smoke is 'doesn't
crash and produces a reasonable shape').

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - ``@pytest.mark.integration`` (custom marker, not run by default)

Skipped without the key. CI never runs this; Berkay runs it
manually after loading the key. The unit tests
(``tests/unit/test_m21_stage.py``) pin all 16 score branches +
telemetry pathways via mocks; this smoke just confirms the wiring
holds against the live UW endpoint.
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
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M21 integration smoke skipped. "
            "Run with the key in .env or export the var to exercise.",
        )
    return SecretStr(raw)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _spy_event(spot: Decimal) -> EnrichedEvent:
    """Build a minimal SPY event for the smoke."""
    op = OptionsPrint(
        event_id="smoke-1",
        timestamp=datetime.now(UTC),
        ticker="SPY",
        option_type="call",
        strike=Decimal("500.00"),
        expiry=date(2099, 12, 31),  # far-future placeholder
        dte=30,
        spot_price=spot,
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
async def test_m21_smoke_against_real_uw_for_spy() -> None:
    """Wire DealerGammaStage to real UW DealerGammaProvider; SPY end-to-end.

    Asserts:
      - The stage's enrich() doesn't raise
      - gamma_score is None OR a float in [0.0, 1.0]
      - last_execution_metadata is populated with a recognised branch label
      - No timeout (UW responds within profile.modules.m21.provider_timeout_s)
    """
    api_key = _key_or_skip()
    client = UnusualWhalesClient(api_key=api_key, settings=_settings())
    provider = UnusualWhalesDealerGammaProvider(
        client=client, settings=_settings(),
    )
    stage = DealerGammaStage(provider=provider)

    try:
        # SPY spot ~500 is a placeholder; live spot is fetched separately
        # in production. For the smoke we just need a valid event shape;
        # the score branch depends on the UW response, not on our spot.
        event = _spy_event(spot=Decimal("500.00"))
        ctx = PipelineContext(profile=load_default_profile())
        await stage.enrich(event, ctx)
    finally:
        await client.aclose()

    # gamma_score is None when UW returns no data, otherwise float [0,1]
    if event.gamma_score is None:
        # Acceptable outcome — UW may not publish GEX for SPY at this
        # exact moment (unlikely but possible). Branch must be 'no_data'.
        assert stage.last_execution_metadata is not None
        assert stage.last_execution_metadata["branch"] == "no_data"
    else:
        assert 0.0 <= event.gamma_score <= 1.0
        assert stage.last_execution_metadata is not None
        # Any non-data branch is fine
        assert stage.last_execution_metadata["branch"] in {
            "full_short_and_proximate",
            "partial_one_condition",
            "no_conditions_met",
            "extreme_distance_cutoff",
        }
        assert stage.last_execution_metadata["provider_returned"] == "yes"
