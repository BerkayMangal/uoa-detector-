"""Phase 3.4.8.1b tests: opening_closing_score field materialization.

Pins:
  - EnrichedEvent has opening_closing_score field (default None)
  - EnrichedEvent has m28_confirmation_score field (default None)
  - StoredSignal has opening_closing_score field (default None)
  - StoredSignal has m28_confirmation_score field (default None)
  - M27 stage writes event.opening_closing_score on each branch
  - M27 stage writes event.opening_closing_score on timeout branch too
  - M27 stage idempotency-on-preset uses opening_closing_score
  - In-memory store.add() copies fields from event
  - SQLite store.add() copies fields from event
  - StoredSignal back-compat: deserializing JSON without these fields
    yields None (no migration needed)
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.providers.open_interest import OpenInterestSnapshot

# ---------------------------------------------------------------------------
# Field shape on EnrichedEvent + StoredSignal
# ---------------------------------------------------------------------------


def _make_event() -> EnrichedEvent:
    op = OptionsPrint(
        event_id="e1",
        timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=Decimal("150.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.25,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("synthetic",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


def test_enriched_event_has_opening_closing_score_field() -> None:
    e = _make_event()
    assert e.opening_closing_score is None
    e.opening_closing_score = 0.7
    assert e.opening_closing_score == 0.7


def test_enriched_event_has_m28_confirmation_score_field() -> None:
    e = _make_event()
    assert e.m28_confirmation_score is None
    e.m28_confirmation_score = 1.0
    assert e.m28_confirmation_score == 1.0


def test_stored_signal_has_new_fields_default_none() -> None:
    """StoredSignal can be instantiated without the new fields (back-compat)."""
    sig = StoredSignal(
        timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("150"),
        expiry=date(2024, 2, 16),
        dte=32,
        premium=Decimal("100000"),
        option_price=Decimal("1.50"),
        moneyness=Decimal("1.0"),
        uoa_score=None,
        convexity_score=None,
        event_score=None,
        gamma_score=None,
        price_confirmation_score=None,
        sector_confirmation_score=None,
        time_of_day_weight=None,
        cluster_density_score=None,
        relative_premium_score=None,
        dte_multiplier_applied=None,
        combined_score_pre_penalty=None,
        combined_score_post_penalty=None,
        sweep_classification=None,
        is_iso=False,
        gamma_flag=False,
        sector_confirmation=False,
        dark_pool_confirmation=False,
        label=SignalLabel.IGNORE_NOISE,
        label_reason="test",
        max_r=0.0,
        scale_in=False,
        initial_r=None,
    )
    assert sig.opening_closing_score is None
    assert sig.m28_confirmation_score is None


def test_stored_signal_back_compat_old_json_deserializes() -> None:
    """JSON without new fields deserializes with None defaults."""
    old_json = '''
    {
      "timestamp": "2024-01-15T15:30:00Z",
      "ticker": "AAPL",
      "option_type": "call",
      "strike": "150",
      "expiry": "2024-02-16",
      "dte": 32,
      "premium": "100000",
      "option_price": "1.50",
      "moneyness": "1.0",
      "uoa_score": null, "convexity_score": null, "event_score": null,
      "gamma_score": null, "price_confirmation_score": null,
      "sector_confirmation_score": null, "time_of_day_weight": null,
      "cluster_density_score": null, "relative_premium_score": null,
      "dte_multiplier_applied": null,
      "combined_score_pre_penalty": null,
      "combined_score_post_penalty": null,
      "penalties_applied": [],
      "contradiction_penalty_applied": false,
      "sweep_classification": null,
      "is_iso": false, "gamma_flag": false,
      "sector_confirmation": false, "dark_pool_confirmation": false,
      "label": "IGNORE_NOISE", "label_reason": "test",
      "max_r": 0.0, "scale_in": false, "initial_r": null
    }
    '''
    sig = StoredSignal.model_validate_json(old_json)
    assert sig.opening_closing_score is None
    assert sig.m28_confirmation_score is None


# ---------------------------------------------------------------------------
# M27 stage writes opening_closing_score
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(
        self,
        *,
        prior: OpenInterestSnapshot | None = None,
        current: OpenInterestSnapshot | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._prior = prior
        self._current = current
        self._delay = delay_seconds

    async def at(  # type: ignore[no-untyped-def]
        self, *, ticker, strike, expiry, option_type, when,
    ):
        del ticker, strike, expiry, option_type
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        # Discriminate by `when`: prior_close_ts is yesterday, event is today
        return (
            self._prior if when.date() != datetime(2024, 1, 15).date()
            else self._current
        )

    async def next_day(  # type: ignore[no-untyped-def]
        self, *, ticker, strike, expiry, option_type, trade_date,
    ):
        return None


def _snap(oi: int) -> OpenInterestSnapshot:
    return OpenInterestSnapshot(
        ticker="AAPL",
        strike=Decimal("150"),
        expiry=date(2024, 2, 16),
        option_type="call",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        open_interest=oi,
    )


@pytest.mark.asyncio
async def test_m27_writes_opening_closing_score_strong_branch() -> None:
    """Strong opening: oi delta > 0.5 → score 1.0 written to event."""
    provider = _StubProvider(
        prior=_snap(1000),
        current=_snap(2000),  # +100% delta
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.opening_closing_score == 1.0


@pytest.mark.asyncio
async def test_m27_writes_opening_closing_score_closing_branch() -> None:
    """Closing: oi delta < -0.1 → score 0.0 written to event."""
    provider = _StubProvider(
        prior=_snap(1000),
        current=_snap(800),  # -20%
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.opening_closing_score == 0.0


@pytest.mark.asyncio
async def test_m27_writes_opening_closing_score_on_timeout() -> None:
    """Timeout still writes opening_closing_score = timeout_score (0.5)."""
    profile = load_default_profile()
    new_m27 = profile.scoring.modules.m27.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m27": new_m27})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(
        prior=_snap(1000),
        current=_snap(2000),
        delay_seconds=0.5,
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    # Score still set despite timeout (telemetry-distinct branch)
    assert event.opening_closing_score == 0.5


@pytest.mark.asyncio
async def test_m27_idempotency_on_preset_skips() -> None:
    """If opening_closing_score already set, M27 short-circuits."""
    provider = _StubProvider(
        prior=_snap(1000),
        current=_snap(2000),
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    event.opening_closing_score = 0.3  # preset
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # Score unchanged (preset_skip branch)
    assert event.opening_closing_score == 0.3
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


# ---------------------------------------------------------------------------
# Stores: add() propagates new fields
# ---------------------------------------------------------------------------


def _decision_and_size() -> tuple[LabelDecision, PositionSize]:
    from uoa_detector.domain.risk import RiskBucket
    return (
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def test_in_memory_store_add_propagates_opening_closing_score() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    store.start_run(profile=profile)
    event = _make_event()
    event.opening_closing_score = 0.7
    decision, size = _decision_and_size()
    sig = store.add(event, decision, size)
    assert sig.opening_closing_score == 0.7
    assert sig.m28_confirmation_score is None
    store.finish_run()
    store.close()


def test_sqlite_store_add_propagates_opening_closing_score() -> None:
    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        store.start_run(profile=profile)
        event = _make_event()
        event.opening_closing_score = 0.7
        event.m28_confirmation_score = 1.0
        decision, size = _decision_and_size()
        sig = store.add(event, decision, size)
        assert sig.opening_closing_score == 0.7
        assert sig.m28_confirmation_score == 1.0
        rid = store.active_run_id
        assert rid is not None
        store.finish_run()
        # Round-trip via iter_records
        records = list(store.iter_records(rid))
        assert len(records) == 1
        assert records[0].opening_closing_score == 0.7
        assert records[0].m28_confirmation_score == 1.0
        store.close()
