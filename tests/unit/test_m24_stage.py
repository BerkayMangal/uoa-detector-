"""Phase 3.4.4.3 tests for ``IVExhaustionStage`` (Module 24).

Pins (acceptance doc §3.4.4):
  Pure helpers:
    - _clamp_iv_rank: < 0 → 0; > 100 → 100; in-range unchanged
    - _score_from_iv_rank: each branch returns expected score
      and label for default thresholds
    - _score_from_iv_rank: profile threshold operator override
      changes branches

  Stage async:
    - Idempotency: M24 ScoreAdjustment present → preset_skip
    - IV provider timeout → branch=iv_provider_timeout, no penalty
    - IV provider returns None → no_iv_history, no penalty
    - IV rank < low_iv → cheap branch, no penalty
    - IV rank in moderate range → moderate branch, no penalty
    - IV rank in elevated range → elevated branch, no penalty
    - IV rank > high_iv but no recent catalyst → expensive
      branch, no penalty
    - IV rank > threshold + recent catalyst → penalty emitted
    - IV rank > 100 (data error) → clamped to 100 → expensive
    - Default constructor uses NoOp providers
    - last_execution_metadata records iv_rank, iv_score, branch,
      penalty flag

  Cross-module (M22 + M24):
    - M22 fires post-event blackout; M24 sees same catalyst → emits
    - M22 fires post-event blackout but M24 sees no catalyst (TTL
      expired between calls) → no emission

  Telemetry:
    - last_execution_metadata round-trips into StageExecutionEntry

  Integration with combined-score formula:
    - When penalty fires, compute_combined_score applies it to pre
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M24Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m22_event_calendar import (
    EventCalendarStage,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import (
    IVExhaustionStage,
    _clamp_iv_rank,
    _score_from_iv_rank,
)
from uoa_detector.providers.catalyst_calendar import (
    CatalystEvent,
    NoOpCatalystCalendarProvider,
)
from uoa_detector.providers.iv_history import (
    IVRankSnapshot,
    NoOpIVHistoryProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    timestamp: datetime | None = None,
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


def _iv_snapshot(iv_rank: float | None) -> IVRankSnapshot:
    return IVRankSnapshot(
        ticker="AAPL",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        option_type="call",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        implied_volatility=0.25,
        iv_rank_252d=iv_rank,
    )


def _catalyst(when: datetime, kind: str = "earnings") -> CatalystEvent:
    return CatalystEvent(  # type: ignore[arg-type]
        ticker="AAPL", kind=kind, when=when, title="Q1",
    )


class _StubIVProvider:
    def __init__(
        self,
        *,
        snapshot: IVRankSnapshot | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._snapshot = snapshot
        self._delay = delay_seconds
        self.call_count = 0

    async def iv_rank_at(  # type: ignore[no-untyped-def]
        self, ticker, strike, expiry, option_type, at,
    ):
        self.call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._snapshot


class _StubCatalystProvider:
    def __init__(
        self,
        *,
        catalysts: tuple[CatalystEvent, ...] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        self._catalysts = catalysts
        self._delay = delay_seconds
        self.call_count = 0

    async def next_catalyst(  # type: ignore[no-untyped-def]
        self, ticker, after,
    ) -> CatalystEvent | None:
        return None

    async def catalysts_in_window(
        self, ticker: str, window_start: datetime, window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        del ticker
        self.call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return tuple(
            c for c in self._catalysts
            if window_start <= c.when <= window_end
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_clamp_iv_rank_in_range_unchanged() -> None:
    assert _clamp_iv_rank(50.0) == 50.0
    assert _clamp_iv_rank(0.0) == 0.0
    assert _clamp_iv_rank(100.0) == 100.0


def test_clamp_iv_rank_above_100_clamped() -> None:
    assert _clamp_iv_rank(105.5) == 100.0


def test_clamp_iv_rank_below_zero_clamped() -> None:
    assert _clamp_iv_rank(-5.0) == 0.0


def test_score_iv_rank_cheap_branch() -> None:
    s = M24Settings()
    score, branch = _score_from_iv_rank(20.0, s)
    assert score == 1.0
    assert branch == "cheap"


def test_score_iv_rank_moderate_branch() -> None:
    s = M24Settings()
    score, branch = _score_from_iv_rank(45.0, s)
    assert score == 0.7
    assert branch == "moderate"


def test_score_iv_rank_elevated_branch() -> None:
    s = M24Settings()
    score, branch = _score_from_iv_rank(70.0, s)
    assert score == 0.3
    assert branch == "elevated"


def test_score_iv_rank_expensive_branch() -> None:
    s = M24Settings()
    score, branch = _score_from_iv_rank(85.0, s)
    assert score == 0.0
    assert branch == "expensive"


def test_score_iv_rank_at_threshold_boundaries() -> None:
    """Ranks exactly at thresholds map to next-higher branch."""
    s = M24Settings()
    # 30.0 == low → moderate (>= low_iv_threshold)
    _score, branch = _score_from_iv_rank(30.0, s)
    assert branch == "moderate"
    # 60.0 == mid → elevated
    _score, branch = _score_from_iv_rank(60.0, s)
    assert branch == "elevated"
    # 80.0 == high → expensive
    _score, branch = _score_from_iv_rank(80.0, s)
    assert branch == "expensive"


def test_score_iv_rank_operator_override_changes_branch() -> None:
    """Tighter low_iv_threshold reclassifies a borderline rank."""
    rank = 25.0
    # Default low=30 → cheap
    _score, branch_default = _score_from_iv_rank(rank, M24Settings())
    assert branch_default == "cheap"
    # Operator low=20 → moderate
    _score, branch_op = _score_from_iv_rank(
        rank, M24Settings(low_iv_threshold=20.0),
    )
    assert branch_op == "moderate"


# ---------------------------------------------------------------------------
# Stage async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_skips_when_m24_adjustment_present() -> None:
    from uoa_detector.domain.agreement import ScoreAdjustment
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    # Pre-populate a M24 adjustment to simulate a hot-swap rerun
    event.score_adjustments.append(ScoreAdjustment(
        target="combined_score_pre",
        delta=-0.4,
        reason="prior run",
        source_module="m24_iv_exhaustion",
    ))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert iv_provider.call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_iv_provider_timeout_no_penalty() -> None:
    profile = load_default_profile()
    new_m24 = profile.scoring.modules.m24.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m24": new_m24})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    iv_provider = _StubIVProvider(
        snapshot=_iv_snapshot(85.0), delay_seconds=0.5,
    )
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "iv_provider_timeout"
    assert len(event.score_adjustments) == 0  # no penalty


@pytest.mark.asyncio
async def test_iv_provider_returns_none_no_iv_history() -> None:
    iv_provider = _StubIVProvider(snapshot=None)
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_iv_history"
    assert len(event.score_adjustments) == 0  # no penalty


@pytest.mark.asyncio
async def test_iv_provider_returns_snapshot_with_no_rank_no_iv_history() -> None:
    """iv_rank_252d=None counts as 'no IV history' even if snapshot exists."""
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(None))
    catalyst_provider = _StubCatalystProvider()
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_iv_history"


@pytest.mark.asyncio
async def test_iv_low_no_penalty() -> None:
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(20.0))
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "cheap"
    assert stage.last_execution_metadata["iv_rank"] == "20.00"
    # Catalyst provider not called since IV not above penalty threshold
    assert catalyst_provider.call_count == 0
    assert len(event.score_adjustments) == 0


@pytest.mark.asyncio
async def test_iv_high_with_recent_catalyst_emits_penalty() -> None:
    """The cross-module trigger: high IV + catalyst within 1 session."""
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "expensive"
    assert (
        stage.last_execution_metadata["post_earnings_penalty_emitted"]
        == "yes"
    )
    assert len(event.score_adjustments) == 1
    adj = event.score_adjustments[0]
    assert adj.target == "combined_score_pre"
    assert adj.delta == -0.4
    assert adj.source_module == "m24_iv_exhaustion"
    assert "85.0" in adj.reason
    assert "80.0" in adj.reason


@pytest.mark.asyncio
async def test_iv_high_but_no_recent_catalyst_no_penalty() -> None:
    """High IV alone does NOT trigger the penalty."""
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))
    catalyst_provider = _StubCatalystProvider(catalysts=())  # empty
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "expensive"
    assert (
        stage.last_execution_metadata["post_earnings_penalty_emitted"]
        == "no"
    )
    assert len(event.score_adjustments) == 0


@pytest.mark.asyncio
async def test_iv_above_100_clamped_to_100() -> None:
    """Provider returns iv_rank > 100 (data error) → clamp to 100."""
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(110.0))
    catalyst_provider = _StubCatalystProvider()
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    # Clamped to 100 → expensive branch
    assert stage.last_execution_metadata["branch"] == "expensive"
    assert stage.last_execution_metadata["iv_rank"] == "100.00"


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_providers() -> None:
    stage = IVExhaustionStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp IV → None → no_iv_history branch
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_iv_history"
    assert isinstance(stage._iv_provider, NoOpIVHistoryProvider)
    assert isinstance(
        stage._catalyst_provider, NoOpCatalystCalendarProvider,
    )


@pytest.mark.asyncio
async def test_catalyst_provider_timeout_no_penalty_but_iv_score_set() -> None:
    """If catalyst provider times out, skip penalty but still record iv branch."""
    profile = load_default_profile()
    new_m24 = profile.scoring.modules.m24.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m24": new_m24})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))
    catalyst_provider = _StubCatalystProvider(
        catalysts=(_catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),),
        delay_seconds=0.5,
    )
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    # IV branch still computed
    assert stage.last_execution_metadata["branch"] == "expensive"
    # No penalty — catalyst provider timed out
    assert (
        stage.last_execution_metadata["post_earnings_penalty_emitted"]
        == "no"
    )
    assert len(event.score_adjustments) == 0


# ---------------------------------------------------------------------------
# Cross-module: M22 + M24 together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_module_m22_post_event_blackout_plus_m24_high_iv_emits_penalty() -> None:
    """M22 fires post-event blackout; M24 sees same catalyst → emits penalty.

    This is the canonical Phase 3.4 cross-module test: two stages
    consume the same catalyst data via the same provider (cache
    serves both); their independent score effects compose
    correctly.
    """
    # Both stages share one catalyst provider (TTL cache means
    # one HTTP fetch serves both)
    yesterday = datetime(2024, 1, 14, 21, 0, tzinfo=UTC)
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=yesterday),
    ))
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))

    m22_stage = EventCalendarStage(provider=catalyst_provider)
    m24_stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )

    event = _make_event()  # event Jan 15
    ctx = PipelineContext(profile=load_default_profile())

    # Run both stages
    await m22_stage.enrich(event, ctx)
    await m24_stage.enrich(event, ctx)

    # M22 effect: event_score = post_event_score = 0.0
    assert event.event_score == 0.0
    assert m22_stage.last_execution_metadata is not None
    assert (
        m22_stage.last_execution_metadata["branch"]
        == "post_event_blackout"
    )

    # M24 effect: high IV + recent catalyst → penalty emitted
    assert m24_stage.last_execution_metadata is not None
    assert m24_stage.last_execution_metadata["branch"] == "expensive"
    assert (
        m24_stage.last_execution_metadata["post_earnings_penalty_emitted"]
        == "yes"
    )
    assert len(event.score_adjustments) == 1
    assert event.score_adjustments[0].source_module == "m24_iv_exhaustion"
    assert event.score_adjustments[0].delta == -0.4


@pytest.mark.asyncio
async def test_cross_module_m22_pre_event_no_m24_penalty() -> None:
    """M22 fires pre-event; M24 only checks PAST → no penalty."""
    # Catalyst is in the FUTURE (5 days out)
    next_week = datetime(2024, 1, 20, 21, 0, tzinfo=UTC)
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=next_week),
    ))
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))

    m22_stage = EventCalendarStage(provider=catalyst_provider)
    m24_stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )

    event = _make_event()  # event Jan 15
    ctx = PipelineContext(profile=load_default_profile())

    await m22_stage.enrich(event, ctx)
    await m24_stage.enrich(event, ctx)

    # M22 fires pre-event branch (catalyst future + DTE survives)
    assert event.event_score == 1.0
    assert m22_stage.last_execution_metadata is not None
    assert (
        m22_stage.last_execution_metadata["branch"]
        == "pre_event_dte_survives"
    )

    # M24: high IV but ONLY a future catalyst → no penalty
    assert m24_stage.last_execution_metadata is not None
    assert m24_stage.last_execution_metadata["branch"] == "expensive"
    assert (
        m24_stage.last_execution_metadata["post_earnings_penalty_emitted"]
        == "no"
    )
    assert len(event.score_adjustments) == 0


# ---------------------------------------------------------------------------
# Telemetry round-trip via orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(75.0))
    catalyst_provider = _StubCatalystProvider()
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "elevated"
    assert entry.metadata["iv_rank"] == "75.00"


# ---------------------------------------------------------------------------
# Integration with combined-score formula
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m24_penalty_reduces_combined_score_pre() -> None:
    """When M24 emits its penalty, compute_combined_score sees -0.4 to pre."""
    from uoa_detector.scoring.combined import compute_combined_score
    iv_provider = _StubIVProvider(snapshot=_iv_snapshot(85.0))
    catalyst_provider = _StubCatalystProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 15, 9, 0, tzinfo=UTC)),
    ))
    stage = IVExhaustionStage(
        iv_provider=iv_provider, catalyst_provider=catalyst_provider,
    )
    event = _make_event()
    # Pre-populate other sub-scores so combined score is computable
    event.uoa_score = 0.5
    event.convexity_score = 0.5
    event.event_score = 0.5
    event.gamma_score = 0.5
    event.price_confirmation_score = 0.5
    event.sector_confirmation_score = 0.5
    event.time_of_day_weight = 0.5
    event.dte_multiplier_applied = 1.0
    profile = load_default_profile()
    ctx = PipelineContext(profile=profile)

    # Run M24 — penalty gets emitted into event.score_adjustments
    await stage.enrich(event, ctx)
    assert len(event.score_adjustments) == 1

    # Compute combined score — should see -0.4 applied to pre
    pre, _post = compute_combined_score(event, profile)
    # Without penalty: pre would be 0.5 (sub-scores * weights, all 0.5)
    # With penalty: pre = 0.5 - 0.4 = 0.1
    assert abs(pre - 0.1) < 1e-9
