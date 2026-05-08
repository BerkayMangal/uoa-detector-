"""Phase 3.4.1.3 tests for ``DealerGammaStage`` (Module 21).

Pins (acceptance doc §3.4.1):
  - Idempotency-on-preset (gamma_score already set → early return)
  - Provider returns None → score=None, branch=no_data
  - Provider timeout → score=0.0, branch=timeout
  - Full score (1.0): short gamma AND proximate
  - Partial score (0.5): short gamma only (NOT proximate)
  - Partial score (0.5): proximate only (NOT short gamma)
  - Zero score (0.0): neither condition met
  - Extreme distance cutoff: distance > extreme_distance_pct → 0.0
  - flip_strike=None handling (no proximity branch testable)
  - last_execution_metadata populated per branch
  - Profile thresholds read (not hardcoded) — operator override changes score
  - Telemetry round-trips through orchestrator into StageExecutionEntry
  - Default constructor (no provider) → NoOp → None score
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M21Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m21_dealer_gamma import (
    DealerGammaStage,
    _score_from_aggregate,
)
from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioning,
    NoOpDealerPositioningProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    spot: Decimal = Decimal("150.00"),
    timestamp: datetime | None = None,
    gamma_score: float | None = None,
) -> EnrichedEvent:
    if timestamp is None:
        timestamp = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    op = OptionsPrint(
        event_id="e1",
        timestamp=timestamp,
        ticker=ticker,
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=spot,
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
    e = EnrichedEvent(print=op)
    if gamma_score is not None:
        e.gamma_score = gamma_score
    return e


class _StubProvider:
    """Configurable DealerPositioningProvider for unit tests."""

    def __init__(
        self,
        *,
        aggregate: DealerExposureAggregate | None = None,
        delay_seconds: float = 0.0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._aggregate = aggregate
        self._delay = delay_seconds
        self._raise = raise_exc
        self.call_count = 0

    async def net_gamma_at(  # type: ignore[no-untyped-def]
        self, ticker, strike, at,
    ) -> DealerPositioning | None:
        return None  # not used by M21

    async def aggregate_for_ticker(
        self, ticker: str, at: datetime,
    ) -> DealerExposureAggregate | None:
        del ticker, at
        self.call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        return self._aggregate


def _make_aggregate(
    *,
    net_gamma: Decimal = Decimal("-100_000_000"),
    flip_strike: Decimal | None = Decimal("150.00"),
) -> DealerExposureAggregate:
    return DealerExposureAggregate(
        ticker="AAPL",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        net_gamma_dollars=net_gamma,
        flip_strike=flip_strike,
    )


# ---------------------------------------------------------------------------
# _score_from_aggregate — pure function branches
# ---------------------------------------------------------------------------


def test_score_full_short_and_proximate() -> None:
    """net_gamma below threshold AND distance < flip_proximity_pct → 1.0."""
    settings = M21Settings()  # defaults: short_gamma=-50M, flip_pct=0.03
    agg = _make_aggregate(
        net_gamma=Decimal("-100_000_000"),  # below -50M ⇒ short
        flip_strike=Decimal("151.00"),       # spot 150 → distance ~0.67%
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 1.0
    assert branch == "full_short_and_proximate"


def test_score_partial_short_gamma_only() -> None:
    """Short gamma, but spot far from flip (within extreme cutoff) → 0.5."""
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("-100_000_000"),
        flip_strike=Decimal("160.00"),  # 6.67% away — past 3% but under 20%
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.5
    assert branch == "partial_one_condition"


def test_score_partial_proximate_only() -> None:
    """Spot near flip, but dealers net long gamma → 0.5."""
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("100_000_000"),   # positive ⇒ NOT short
        flip_strike=Decimal("150.50"),       # 0.33% ⇒ proximate
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.5
    assert branch == "partial_one_condition"


def test_score_zero_neither_condition() -> None:
    """Long gamma + far from flip (within cutoff) → 0.0."""
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("100_000_000"),
        flip_strike=Decimal("155.00"),  # 3.33% — past 3% threshold
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.0
    assert branch == "no_conditions_met"


def test_score_extreme_distance_hard_cutoff() -> None:
    """Even with short gamma, distance > 20% collapses to 0.0."""
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("-100_000_000"),  # short
        flip_strike=Decimal("190.00"),       # 26.67% from spot 150
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.0
    assert branch == "extreme_distance_cutoff"


def test_score_flip_strike_none_short_gamma_collapses_partial() -> None:
    """flip_strike=None: proximity unmeasurable; short-gamma alone → 0.5."""
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("-100_000_000"),
        flip_strike=None,
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.5
    assert branch == "partial_one_condition"


def test_score_flip_strike_none_long_gamma_zero() -> None:
    settings = M21Settings()
    agg = _make_aggregate(
        net_gamma=Decimal("100_000_000"),
        flip_strike=None,
    )
    score, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=settings,
    )
    assert score == 0.0
    assert branch == "no_conditions_met"


def test_score_profile_threshold_read_not_hardcoded() -> None:
    """Operator override of short_gamma_threshold changes the score branch."""
    # Net gamma -10M: above default -50M (NOT short), but below operator -5M (short)
    agg = _make_aggregate(
        net_gamma=Decimal("-10_000_000"),
        flip_strike=Decimal("150.50"),  # proximate
    )
    # Default profile: -10M is NOT below -50M → not short → partial (proximate only)
    default_settings = M21Settings()
    score_default, _ = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=default_settings,
    )
    assert score_default == 0.5
    # Operator profile: -5M threshold → -10M IS below → short → full
    operator_settings = M21Settings(
        short_gamma_threshold=Decimal("-5_000_000"),
    )
    score_op, _ = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=operator_settings,
    )
    assert score_op == 1.0


def test_score_profile_extreme_distance_pct_read() -> None:
    """Operator override of extreme_distance_pct changes the cutoff."""
    agg = _make_aggregate(
        net_gamma=Decimal("-100_000_000"),
        flip_strike=Decimal("160.00"),  # 6.67% — under default 20%
    )
    # Default: 6.67% is partial (NOT past 20% cutoff)
    score_default, _ = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"), settings=M21Settings(),
    )
    assert score_default == 0.5  # short gamma only; partial
    # Tight operator cutoff at 5% — 6.67% past it → cutoff 0.0
    score_tight, branch = _score_from_aggregate(
        aggregate=agg, spot=Decimal("150.00"),
        settings=M21Settings(extreme_distance_pct=0.05),
    )
    assert score_tight == 0.0
    assert branch == "extreme_distance_cutoff"


# ---------------------------------------------------------------------------
# Stage — async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_on_preset_skips_provider() -> None:
    """If gamma_score is already set, the provider is never called."""
    provider = _StubProvider(aggregate=_make_aggregate())
    stage = DealerGammaStage(provider=provider)
    event = _make_event(gamma_score=0.7)
    ctx = PipelineContext(profile=load_default_profile())
    result = await stage.enrich(event, ctx)
    assert result.gamma_score == 0.7  # unchanged
    assert provider.call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_provider_returns_none_score_stays_none() -> None:
    provider = _StubProvider(aggregate=None)
    stage = DealerGammaStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.gamma_score is None
    assert provider.call_count == 1
    assert stage.last_execution_metadata == {
        "branch": "no_data",
        "provider_returned": "no",
    }


@pytest.mark.asyncio
async def test_provider_timeout_emits_zero_score() -> None:
    """Long-running provider call exceeds timeout → score=0.0, branch=timeout."""
    # Profile timeout = 2.0s default; stub delay 5s would force timeout
    # Use a short timeout via custom profile
    profile = load_default_profile()
    # Mutate the loaded model: M21Settings is frozen=False (no _StrictModel
    # frozen=True declared); use model_copy with update.
    new_m21 = profile.scoring.modules.m21.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m21": new_m21})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(aggregate=_make_aggregate(), delay_seconds=0.5)
    stage = DealerGammaStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.gamma_score == 0.0
    assert stage.last_execution_metadata == {
        "branch": "timeout",
        "provider_returned": "no",
    }


@pytest.mark.asyncio
async def test_full_score_branch_via_stage() -> None:
    """End-to-end: short gamma + proximate → score=1.0 + branch label."""
    provider = _StubProvider(aggregate=_make_aggregate(
        net_gamma=Decimal("-100_000_000"),
        flip_strike=Decimal("151.00"),
    ))
    stage = DealerGammaStage(provider=provider)
    event = _make_event(spot=Decimal("150.00"))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.gamma_score == 1.0
    assert stage.last_execution_metadata == {
        "branch": "full_short_and_proximate",
        "provider_returned": "yes",
    }


@pytest.mark.asyncio
async def test_zero_score_branch_via_stage() -> None:
    """End-to-end: long gamma + far from flip → score=0.0."""
    provider = _StubProvider(aggregate=_make_aggregate(
        net_gamma=Decimal("100_000_000"),
        flip_strike=Decimal("155.00"),
    ))
    stage = DealerGammaStage(provider=provider)
    event = _make_event(spot=Decimal("150.00"))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.gamma_score == 0.0
    assert stage.last_execution_metadata == {
        "branch": "no_conditions_met",
        "provider_returned": "yes",
    }


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_provider() -> None:
    """No provider arg → NoOp default → score stays None."""
    stage = DealerGammaStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.gamma_score is None
    assert isinstance(stage._provider, NoOpDealerPositioningProvider)


# ---------------------------------------------------------------------------
# Telemetry through orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    """Stage's last_execution_metadata is copied into StageExecutionEntry."""
    from uoa_detector.observability.decision_record import StageExecutionEntry

    provider = _StubProvider(aggregate=_make_aggregate(
        net_gamma=Decimal("-100_000_000"),
        flip_strike=Decimal("151.00"),
    ))
    stage = DealerGammaStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # Simulate the orchestrator copy step
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=1.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata == {
        "branch": "full_short_and_proximate",
        "provider_returned": "yes",
    }
