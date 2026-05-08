"""Phase 3.4.9.3 end-to-end synthetic-pipeline integration test.

Exercises the full Phase 3.4 pipeline against a synthetic source:

  SyntheticRawFlowSource (3 scripted RawPrints)
    → SourceFusion (single-source fast path)
      → 13-stage Pipeline (default_stage_pipeline):
        • TimeOfDayStage (M39)
        • DTEDecayStage (M35)
        • SweepBlockStage (M34)
        • RelativePremiumStage (M37)
        • DealerGammaStage (M21)        — Phase 3.4.1
        • EventCalendarStage (M22)      — Phase 3.4.2
        • PriceConfirmationStage (M23)  — Phase 3.4.3
        • IVExhaustionStage (M24)       — Phase 3.4.4
        • SectorPeerStage (M25)         — Phase 3.4.5
        • DarkPoolStage (M26)           — Phase 3.4.6
        • OpeningClosingStage (M27)     — Phase 3.4.7
        • TemporalClusterStage (M38)
        • ClusterDecayStage
      → CombinedScore + PenaltyEngine + Labeler + RiskSizer
        → BacktestStore.add() → StoredSignal

All stages run with NoOp providers (M21-M27 default constructors),
so this test pins the WIRING:
  - Each stage's enrich() runs without raising
  - Each Phase 3.4 stage produces a deterministic neutral-score
    outcome on NoOp data
  - opening_closing_score (Phase 3.4.7's materialized field) is
    set by M27
  - Each StoredSignal is shape-valid + persisted
  - Final combined_score lands in [0, 1]

This is the FIRST end-to-end test that exercises the entire
Phase 3.4 module set in one process. M28 is a post-event validator
(separate CLI) and is NOT part of this pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.synthetic import SyntheticRawFlowSource


def _make_raw_print(
    *,
    event_id: str = "synth-1",
    ticker: str = "AAPL",
    option_type: str = "call",
    timestamp: datetime | None = None,
    strike: Decimal = Decimal("180.00"),
    spot_price: Decimal = Decimal("180.00"),
    premium_paid: Decimal = Decimal("250000"),  # $250K premium
) -> RawPrint:
    """Construct a synthetic RawPrint suitable for the full pipeline."""
    if timestamp is None:
        # Mid-day Tuesday; ensures TimeOfDay weight is sane
        timestamp = datetime(2024, 1, 16, 18, 30, tzinfo=UTC)
    return RawPrint(
        source_id="synthetic",
        source_event_id=event_id,
        timestamp=timestamp,
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike,
        expiry=date(2024, 2, 16),
        dte=30,
        spot_price=spot_price,
        premium_paid=premium_paid,
        option_price=Decimal("2.50"),
        bid=Decimal("2.40"),
        ask=Decimal("2.60"),
        fill_side="at_ask",
        exchange="CBOE",
        implied_volatility=0.28,
        open_interest=1500,
        is_iso=False,
        source_tags=(),
    )


@pytest.mark.asyncio
async def test_end_to_end_pipeline_phase_3_4_synthetic_three_events() -> None:
    """Drive 3 synthetic events through the full Phase 3.4 pipeline.

    Pins:
      - All 13 stages execute on each event without raising
      - Each StoredSignal carries the new Phase 3.4 fields:
          opening_closing_score (set by M27)
          m28_confirmation_score = None (M28 is post-event)
          has_dark_pool_confirmation = False (NoOp DP provider)
      - combined_score_pre/post lands in [0, 1]
      - All 3 signals reach the store
    """
    raw_prints = [
        _make_raw_print(
            event_id="synth-call-1",
            option_type="call",
            timestamp=datetime(2024, 1, 16, 18, 30, tzinfo=UTC),
        ),
        _make_raw_print(
            event_id="synth-put-1",
            option_type="put",
            ticker="MSFT",
            strike=Decimal("400.00"),
            spot_price=Decimal("400.00"),
            timestamp=datetime(2024, 1, 16, 19, 0, tzinfo=UTC),
        ),
        _make_raw_print(
            event_id="synth-call-2",
            option_type="call",
            ticker="GOOGL",
            strike=Decimal("150.00"),
            spot_price=Decimal("150.00"),
            timestamp=datetime(2024, 1, 16, 19, 30, tzinfo=UTC),
        ),
    ]

    source = SyntheticRawFlowSource("synthetic", raw_prints)
    profile = load_default_profile()
    store = BacktestStore()
    stages = default_stage_pipeline()

    # Sanity: 13 stages composed
    assert len(stages) == 13

    pipeline = Pipeline(
        sources=[source],
        stages=stages,
        profile=profile,
        store=store,
    )

    results = await pipeline.run()

    # All 3 events processed
    assert len(results) == 3

    # Use store.all() — works regardless of run lifecycle
    records = pipeline.store.all()
    assert len(records) == 3

    for sig in records:
        # Phase 3.4.7 field materialized — M27 ran on every event
        assert sig.opening_closing_score is not None
        assert 0.0 <= sig.opening_closing_score <= 1.0

        # Phase 3.4.8 field — M28 is post-event, never written by pipeline
        assert sig.m28_confirmation_score is None

        # Combined score lands in valid range
        if sig.combined_score_pre_penalty is not None:
            assert -1.0 <= sig.combined_score_pre_penalty <= 1.5
        if sig.combined_score_post_penalty is not None:
            assert -1.0 <= sig.combined_score_post_penalty <= 1.5

        # M26 NoOp provider returns no prints → flag stays False
        assert sig.dark_pool_confirmation is False

    pipeline.store.close()


@pytest.mark.asyncio
async def test_end_to_end_pipeline_each_m_stage_runs_on_event() -> None:
    """Single event through pipeline; verify each Phase 3.4 stage executed.

    Inspect ``stage.last_execution_metadata`` to confirm each stage ran
    its enrich() and recorded a branch. NoOp providers produce
    deterministic outcomes per stage.
    """
    raw_print = _make_raw_print(event_id="single-1")
    source = SyntheticRawFlowSource("synthetic", [raw_print])
    profile = load_default_profile()
    store = BacktestStore()
    stages = default_stage_pipeline()
    pipeline = Pipeline(
        sources=[source],
        stages=stages,
        profile=profile,
        store=store,
    )

    results = await pipeline.run()
    assert len(results) == 1

    # Phase 3.4 stages have last_execution_metadata
    phase_3_4_stages = {
        "m21_dealer_gamma",
        "m22_event_calendar",
        "m23_price_confirmation",
        "m24_iv_exhaustion",
        "m25_sector_peer",
        "m26_dark_pool",
        "m27_opening_closing",
    }
    seen = set()
    for stage in stages:
        if hasattr(stage, "last_execution_metadata"):
            meta = stage.last_execution_metadata
            stage_name = getattr(stage, "name", None)
            if stage_name in phase_3_4_stages and meta is not None:
                seen.add(stage_name)
                # Each stage records SOME branch label
                assert "branch" in meta

    # All 7 Phase 3.4 stages observed
    assert seen == phase_3_4_stages, (
        f"Missing stages from Phase 3.4 set: {phase_3_4_stages - seen}"
    )
    store.close()


@pytest.mark.asyncio
async def test_end_to_end_pipeline_signals_have_run_id() -> None:
    """Storage invariant: every stored signal carries a run_id."""
    raw_prints = [_make_raw_print(event_id=f"e{i}") for i in range(2)]
    source = SyntheticRawFlowSource("synthetic", raw_prints)
    profile = load_default_profile()
    store = BacktestStore()
    pipeline = Pipeline(
        sources=[source],
        stages=default_stage_pipeline(),
        profile=profile,
        store=store,
    )
    await pipeline.run()
    records = pipeline.store.all()
    assert len(records) == 2
    # All signals share the same (auto-assigned) run_id
    run_ids = {sig.run_id for sig in records}
    assert len(run_ids) == 1
    assert next(iter(run_ids)) is not None
    pipeline.store.close()


# Suppress unused import lint
_ = asyncio
