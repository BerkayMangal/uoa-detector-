"""Phase 3.4.3.3 tests for ``PriceConfirmationStage`` (Module 23).

Pins (acceptance doc §3.4.3):
  Pure _score_from_movement function:
    - call + spot up > threshold → confirmed (1.0)
    - call + spot down > threshold → contrarian (0.3)
    - call + small move → neutral (0.5)
    - put + spot down > threshold → confirmed (1.0)
    - put + spot up > threshold → contrarian (0.3)
    - put + small move → neutral (0.5)
    - unknown option_type → neutral_unknown_type
    - profile threshold operator override changes branch

  _compute_effective_lookback session-boundary clamp:
    - lookback fits inside session → unchanged
    - lookback crosses session_open → clamped to minutes_to_open
    - event_ts at session_open exactly → clamp to 1 minute (min)
    - pre-market event_ts (before session_open) → no clamp

  Stage async:
    - idempotency-on-preset (provider not called)
    - timeout → score=neutral, branch=timeout
    - no spot data → score=neutral, branch=data_missing_neutral
    - end-to-end call_confirmed branch
    - end-to-end put_confirmed branch
    - end-to-end call_contrarian branch
    - end-to-end neutral branch
    - default constructor uses NoOp provider

  Telemetry:
    - last_execution_metadata populated per branch with
      lookback_minutes_used + move_pct
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M23Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
    _compute_effective_lookback,
    _score_from_movement,
)
from uoa_detector.providers.price_action import (
    NoOpPriceActionProvider,
    PriceMovement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    option_type: str = "call",
    timestamp: datetime | None = None,
    price_confirmation_score: float | None = None,
) -> EnrichedEvent:
    if timestamp is None:
        timestamp = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    op = OptionsPrint(
        event_id="e1",
        timestamp=timestamp,
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
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
    e = EnrichedEvent(print=op)
    if price_confirmation_score is not None:
        e.price_confirmation_score = price_confirmation_score
    return e


def _movement(
    *,
    move_pct: float = 0.0,
    spot_at: Decimal = Decimal("150.00"),
    spot_lookback_ago: Decimal = Decimal("150.00"),
    lookback_actual: int = 30,
) -> PriceMovement:
    return PriceMovement(
        ticker="AAPL",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        spot_at=spot_at,
        spot_lookback_ago=spot_lookback_ago,
        move_pct=move_pct,
        lookback_minutes_actual=lookback_actual,
    )


class _StubProvider:
    """Configurable PriceActionProvider for M23 tests."""

    def __init__(
        self,
        *,
        movement: PriceMovement | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._movement = movement
        self._delay = delay_seconds
        self.call_count = 0
        self.last_lookback: int | None = None

    async def snapshot_at(self, ticker, at):  # type: ignore[no-untyped-def]
        return None

    async def get_intraday_price_movement(
        self,
        ticker: str,
        at: datetime,
        lookback_minutes: int,
    ) -> PriceMovement | None:
        del ticker, at
        self.call_count += 1
        self.last_lookback = lookback_minutes
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._movement


# ---------------------------------------------------------------------------
# _score_from_movement — pure function branches
# ---------------------------------------------------------------------------


def test_score_call_spot_up_above_threshold_confirmed() -> None:
    """call + move 1% (>0.5%) → confirmed = 1.0."""
    settings = M23Settings()  # confirmation_pct=0.005
    score, branch = _score_from_movement(
        movement=_movement(move_pct=0.01),
        option_type="call",
        settings=settings,
    )
    assert score == 1.0
    assert branch == "call_confirmed"


def test_score_call_spot_down_above_threshold_contrarian() -> None:
    """call + move -1% → contrarian = 0.3."""
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=-0.01),
        option_type="call",
        settings=settings,
    )
    assert score == 0.3
    assert branch == "call_contrarian"


def test_score_call_small_move_neutral() -> None:
    """call + |move| < threshold → neutral = 0.5."""
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=0.001),
        option_type="call",
        settings=settings,
    )
    assert score == 0.5
    assert branch == "neutral"


def test_score_put_spot_down_above_threshold_confirmed() -> None:
    """put + spot fell 1% → confirmed = 1.0."""
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=-0.01),
        option_type="put",
        settings=settings,
    )
    assert score == 1.0
    assert branch == "put_confirmed"


def test_score_put_spot_up_above_threshold_contrarian() -> None:
    """put + spot rose 1% → contrarian = 0.3."""
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=0.01),
        option_type="put",
        settings=settings,
    )
    assert score == 0.3
    assert branch == "put_contrarian"


def test_score_put_small_move_neutral() -> None:
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=-0.001),
        option_type="put",
        settings=settings,
    )
    assert score == 0.5
    assert branch == "neutral"


def test_score_unknown_option_type_returns_neutral_unknown() -> None:
    """Defensive: unknown option_type → neutral with distinct branch label."""
    settings = M23Settings()
    score, branch = _score_from_movement(
        movement=_movement(move_pct=0.01),
        option_type="banana",  # not call/put
        settings=settings,
    )
    assert score == 0.5
    assert branch == "neutral_unknown_type"


def test_score_profile_threshold_operator_override() -> None:
    """Operator can pin a tighter or wider confirmation threshold."""
    move = 0.003  # 0.3%
    # Default 0.5% → neutral (0.003 < 0.005)
    _score_default, branch_default = _score_from_movement(
        movement=_movement(move_pct=move),
        option_type="call",
        settings=M23Settings(),
    )
    assert branch_default == "neutral"
    # Tighter operator threshold 0.2% → confirmed
    score_tight, branch_tight = _score_from_movement(
        movement=_movement(move_pct=move),
        option_type="call",
        settings=M23Settings(confirmation_pct=0.002),
    )
    assert branch_tight == "call_confirmed"
    assert score_tight == 1.0


def test_score_exact_threshold_is_neutral() -> None:
    """move == threshold (not strictly greater) → neutral."""
    settings = M23Settings()  # confirmation_pct=0.005
    _score, branch = _score_from_movement(
        movement=_movement(move_pct=0.005),  # exactly the threshold
        option_type="call",
        settings=settings,
    )
    # Strictly greater required for confirmed
    assert branch == "neutral"


# ---------------------------------------------------------------------------
# _compute_effective_lookback — session clamp
# ---------------------------------------------------------------------------


def test_clamp_lookback_fits_inside_session_unchanged() -> None:
    """event 15:30, session_open 14:30 (60 min window), lookback 30 → unchanged."""
    out = _compute_effective_lookback(
        event_ts=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        requested_lookback=30,
        session_open_hour=14,
        session_open_minute=30,
    )
    assert out == 30


def test_clamp_lookback_crosses_session_open_clamped() -> None:
    """event 14:45, session_open 14:30, lookback 30 → clamped to 15."""
    out = _compute_effective_lookback(
        event_ts=datetime(2024, 1, 15, 14, 45, tzinfo=UTC),
        requested_lookback=30,
        session_open_hour=14,
        session_open_minute=30,
    )
    assert out == 15


def test_clamp_lookback_event_at_session_open_exact() -> None:
    """event exactly at session_open → minutes_to_open = 0 → clamp to 1 min min."""
    out = _compute_effective_lookback(
        event_ts=datetime(2024, 1, 15, 14, 30, tzinfo=UTC),
        requested_lookback=30,
        session_open_hour=14,
        session_open_minute=30,
    )
    assert out == 1  # at least 1 min so provider has a window


def test_clamp_pre_market_event_no_clamp() -> None:
    """event before session_open → don't clamp (no session boundary in past)."""
    out = _compute_effective_lookback(
        event_ts=datetime(2024, 1, 15, 13, 0, tzinfo=UTC),  # 1 hour before open
        requested_lookback=30,
        session_open_hour=14,
        session_open_minute=30,
    )
    assert out == 30  # unchanged


def test_clamp_lookback_at_max_session_minute_unchanged() -> None:
    """event 16:00, session_open 14:30, lookback 60 → 60 (fits)."""
    out = _compute_effective_lookback(
        event_ts=datetime(2024, 1, 15, 16, 0, tzinfo=UTC),
        requested_lookback=60,
        session_open_hour=14,
        session_open_minute=30,
    )
    assert out == 60


# ---------------------------------------------------------------------------
# Stage — async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_on_preset_skips_provider() -> None:
    provider = _StubProvider(movement=_movement(move_pct=0.01))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(price_confirmation_score=0.8)
    ctx = PipelineContext(profile=load_default_profile())
    result = await stage.enrich(event, ctx)
    assert result.price_confirmation_score == 0.8
    assert provider.call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_timeout_emits_neutral_score() -> None:
    profile = load_default_profile()
    new_m23 = profile.scoring.modules.m23.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m23": new_m23})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(
        movement=_movement(move_pct=0.01),
        delay_seconds=0.5,
    )
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 0.5  # neutral_score
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "timeout"
    assert stage.last_execution_metadata["provider_returned"] == "no"


class _RaisingProvider:
    """Provider that raises a configurable exception. Phase 3.3.8.1 fix."""

    def __init__(self, *, exc: BaseException) -> None:
        self._exc = exc
        self.call_count = 0

    async def snapshot_at(self, ticker, at):  # type: ignore[no-untyped-def]
        return None

    async def get_intraday_price_movement(
        self,
        ticker: str,
        at: datetime,
        lookback_minutes: int,
    ) -> PriceMovement | None:
        del ticker, at, lookback_minutes
        self.call_count += 1
        raise self._exc


@pytest.mark.asyncio
async def test_provider_exception_emits_neutral_score() -> None:
    """Phase 3.3.8.1: provider exception → neutral score, branch=provider_error.

    Before the fix, M23 only caught TimeoutError. A subscription / auth
    failure (e.g. ThetaData STOCK.VALUE missing) raised an HTTP error
    that crashed the pipeline. After the fix, generic Exception is
    caught and mapped to the same neutral fallback as no-data, with a
    distinct branch label and error_type telemetry.
    """
    provider = _RaisingProvider(
        exc=RuntimeError("simulated HTTP 403 subscription required"),
    )
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 0.5  # neutral_score
    assert provider.call_count == 1
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "provider_error"
    assert stage.last_execution_metadata["provider_returned"] == "no"
    assert stage.last_execution_metadata["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_provider_baseexception_propagates() -> None:
    """KeyboardInterrupt / SystemExit / asyncio.CancelledError must propagate.

    The 3.3.8.1 fix catches Exception (not BaseException). Verifies
    that asyncio cancellation isn't accidentally swallowed.
    """
    provider = _RaisingProvider(exc=asyncio.CancelledError())
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    with pytest.raises(asyncio.CancelledError):
        await stage.enrich(event, ctx)


@pytest.mark.asyncio
async def test_no_spot_data_emits_neutral_score() -> None:
    """Provider returns None → neutral fallback per acceptance doc edge case."""
    provider = _StubProvider(movement=None)
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 0.5
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "data_missing_neutral"
    assert stage.last_execution_metadata["provider_returned"] == "no"


@pytest.mark.asyncio
async def test_end_to_end_call_confirmed() -> None:
    provider = _StubProvider(movement=_movement(move_pct=0.012))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "call_confirmed"
    assert stage.last_execution_metadata["provider_returned"] == "yes"
    assert "move_pct" in stage.last_execution_metadata


@pytest.mark.asyncio
async def test_end_to_end_put_confirmed() -> None:
    provider = _StubProvider(movement=_movement(move_pct=-0.012))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(option_type="put")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "put_confirmed"


@pytest.mark.asyncio
async def test_end_to_end_call_contrarian() -> None:
    provider = _StubProvider(movement=_movement(move_pct=-0.015))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 0.3
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "call_contrarian"


@pytest.mark.asyncio
async def test_end_to_end_neutral() -> None:
    provider = _StubProvider(movement=_movement(move_pct=0.001))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.price_confirmation_score == 0.5
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "neutral"


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_provider() -> None:
    stage = PriceConfirmationStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp returns None → neutral_score = 0.5
    assert event.price_confirmation_score == 0.5
    assert isinstance(stage._provider, NoOpPriceActionProvider)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "data_missing_neutral"


@pytest.mark.asyncio
async def test_session_clamp_passed_to_provider() -> None:
    """When event_ts is just past session_open, lookback gets clamped."""
    provider = _StubProvider(movement=_movement(move_pct=0.01, lookback_actual=15))
    stage = PriceConfirmationStage(provider=provider)
    # Event at 14:45, session_open 14:30 → 15 min to open; lookback 30 → clamped 15
    event = _make_event(timestamp=datetime(2024, 1, 15, 14, 45, tzinfo=UTC))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert provider.last_lookback == 15  # clamped from 30


# ---------------------------------------------------------------------------
# Telemetry round-trip via orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry

    provider = _StubProvider(movement=_movement(move_pct=0.012))
    stage = PriceConfirmationStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "call_confirmed"
    assert entry.metadata["provider_returned"] == "yes"
