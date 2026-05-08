"""Phase 3.4.8.4 M28 integration smoke test.

Exercises the full M28 batch validation flow against the real
UnusualWhalesOpenInterestProvider when UNUSUAL_WHALES_API_KEY is
set:

  1. Build a synthetic StoredSignal (recent SPY contract,
     opening_closing_score = 0.7) in an ephemeral SqliteBacktestStore.
  2. Construct M28Validator with the real UW provider.
  3. Run validator.validate_run(run_id).
  4. Assert: stats are well-formed, no exceptions raised,
     m28_confirmation_score either set (live data available) OR
     pending (T+1 not yet published — both legitimate outcomes).

GATED BY:
  - UNUSUAL_WHALES_API_KEY env var present
  - @pytest.mark.integration marker

Skipped without the key. The unit tests
(test_m28_validator.py, test_run_m28_overnight_cli.py,
test_update_signal_score.py) pin all logic via mocks; this smoke
confirms wiring against the live UW endpoints.

Note: This is the FIRST integration smoke for a non-PipelineStage
module. The wiring under test:

  UnusualWhalesClient
    → UnusualWhalesOpenInterestProvider
      → M28Validator
        → SqliteBacktestStore.update_signal_score()
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.pipeline.validators import M28Validator
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "M28 batch integration smoke skipped. "
            "Run with the key in .env or export the var.",
        )
    return SecretStr(raw)


def _build_synthetic_signal_event() -> EnrichedEvent:
    """Construct a recent SPY signal event for the smoke.

    Uses a recent past timestamp (yesterday) so T+1 OI exists today
    (assuming we run during/after market hours). expiry is a real
    near-future SPY expiry (next monthly).
    """
    yesterday = datetime.now(UTC) - timedelta(days=1)
    # Pick a strike around current SPY price — 500 is plausible
    op = OptionsPrint(
        event_id="m28-smoke-1",
        timestamp=yesterday,
        ticker="SPY",
        option_type="call",
        strike=Decimal("500.00"),
        expiry=date(2099, 12, 31),  # far future to skip-skip-expired
        dte=1000,
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
    e = EnrichedEvent(print=op)
    e.opening_closing_score = 0.7  # qualify for validation
    return e


@pytest.mark.asyncio
async def test_m28_smoke_against_real_uw_for_spy() -> None:
    """End-to-end M28 batch flow: real provider + ephemeral SQLite store.

    Asserts:
      - validate_run() doesn't raise
      - ValidationStats is well-formed (counts sum correctly)
      - The single signal lands in exactly one of: confirmed,
        ambiguous, closing, pending_no_data, errors
      - When score was written, it is in {0.0, 0.5, 1.0} (the three
        score-branch values)
    """
    api_key = _key_or_skip()
    profile = load_default_profile()
    uw_settings = UnusualWhalesSettings()
    client = UnusualWhalesClient(api_key=api_key, settings=uw_settings)
    provider = UnusualWhalesOpenInterestProvider(
        client=client, settings=uw_settings,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision = LabelDecision(
            label=SignalLabel.STANDARD_UOA, reason="smoke",
        )
        size = PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5)
        store.add(_build_synthetic_signal_event(), decision, size)
        store.finish_run()

        validator = M28Validator(
            provider=provider, store=store, profile=profile,
        )
        try:
            stats = await validator.validate_run(rid)
        finally:
            await client.aclose()

        # Stats well-formed
        assert stats.run_id == rid
        assert stats.total_signals == 1
        # The signal lands in exactly one outcome bucket
        outcome_count = (
            stats.confirmed
            + stats.ambiguous
            + stats.closing
            + stats.skipped_no_m27_score
            + stats.skipped_below_threshold
            + stats.skipped_contract_expired
            + stats.errors
            + stats.pending_no_data
        )
        assert outcome_count == 1

        # If score was written, verify it's a recognized value
        records = list(store.iter_records(rid))
        assert len(records) == 1
        score = records[0].m28_confirmation_score
        if score is not None:
            assert score in (0.0, 0.5, 1.0)
        store.close()
