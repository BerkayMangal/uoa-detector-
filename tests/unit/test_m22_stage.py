"""Phase 3.4.2.3 tests for ``EventCalendarStage`` (Module 22).

Pins (acceptance doc §3.4.2):
  Pure _score_from_catalysts function:
    - empty window → no_catalyst_neutral_score (no_catalyst)
    - same-day catalyst → post_event_blackout (precedence)
    - past catalyst within blackout → post_event_blackout
    - future catalyst, DTE > days_to → dte_survives_score
    - future catalyst, DTE < days_to → dte_expires_before_score
    - future catalyst, expiry == catalyst_date → expires_before
      (option stops trading at announcement)
    - multiple catalysts: post-event has priority over pre-event
    - multiple future catalysts: closest in time wins
    - profile threshold operator override changes branch

  Stage async:
    - idempotency-on-preset (provider not called)
    - timeout → score=0.0, branch=timeout
    - end-to-end no_catalyst branch
    - end-to-end pre_event_dte_survives branch
    - end-to-end post_event_blackout branch
    - default constructor uses NoOp provider

  Telemetry:
    - last_execution_metadata populated per branch (with
      catalysts_count + provider_returned fields)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M22Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m22_event_calendar import (
    EventCalendarStage,
    _score_from_catalysts,
)
from uoa_detector.providers.catalyst_calendar import (
    CatalystEvent,
    NoOpCatalystCalendarProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    timestamp: datetime | None = None,
    expiry: date | None = None,
    event_score: float | None = None,
) -> EnrichedEvent:
    if timestamp is None:
        timestamp = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    if expiry is None:
        expiry = date(2024, 2, 16)
    op = OptionsPrint(
        event_id="e1",
        timestamp=timestamp,
        ticker=ticker,
        option_type="call",
        strike=Decimal("150.00"),
        expiry=expiry,
        dte=(expiry - timestamp.date()).days,
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
    if event_score is not None:
        e.event_score = event_score
    return e


def _catalyst(when: datetime, kind: str = "earnings", title: str = "Q1") -> CatalystEvent:
    return CatalystEvent(ticker="AAPL", kind=kind, when=when, title=title)  # type: ignore[arg-type]


class _StubProvider:
    """Configurable CatalystCalendarProvider for unit tests."""

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
        return None  # not used by M22

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
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
# _score_from_catalysts — pure function branches
# ---------------------------------------------------------------------------


def test_score_empty_window_returns_neutral() -> None:
    settings = M22Settings()  # no_catalyst_neutral_score=0.3
    score, branch = _score_from_catalysts(
        catalysts=(),
        event_ts=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert score == 0.3
    assert branch == "no_catalyst"


def test_score_same_day_catalyst_is_post_event_blackout() -> None:
    """Acceptance doc: 'Catalyst on same day as event → treat as post-event blackout'."""
    settings = M22Settings()  # post_event_score=0.0, blackout_days=1
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    same_day_catalyst = _catalyst(
        when=datetime(2024, 1, 15, 21, 0, tzinfo=UTC),  # same date
    )
    score, branch = _score_from_catalysts(
        catalysts=(same_day_catalyst,),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert score == 0.0
    assert branch == "post_event_blackout"


def test_score_past_catalyst_within_blackout_window() -> None:
    """A catalyst yesterday (blackout_days=1) is post-event blackout."""
    settings = M22Settings()  # blackout_days=1
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    yesterday = _catalyst(when=datetime(2024, 1, 14, 21, 0, tzinfo=UTC))
    score, branch = _score_from_catalysts(
        catalysts=(yesterday,),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert score == 0.0
    assert branch == "post_event_blackout"


def test_score_future_catalyst_dte_survives_full_score() -> None:
    """Future catalyst, option expiry > catalyst date → 1.0."""
    settings = M22Settings()  # dte_survives_score=1.0
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    future = _catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC))
    score, branch = _score_from_catalysts(
        catalysts=(future,),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),  # past Jan 25 — survives
        settings=settings,
    )
    assert score == 1.0
    assert branch == "pre_event_dte_survives"


def test_score_future_catalyst_dte_expires_before_partial() -> None:
    """Future catalyst, option expiry < catalyst date → 0.5."""
    settings = M22Settings()  # dte_expires_before_score=0.5
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    future = _catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC))
    score, branch = _score_from_catalysts(
        catalysts=(future,),
        event_ts=event_ts,
        expiry=date(2024, 1, 19),  # before Jan 25 — expires before
        settings=settings,
    )
    assert score == 0.5
    assert branch == "pre_event_dte_expires_before"


def test_score_expiry_equals_catalyst_date_treated_as_expires_before() -> None:
    """Same-date expiry: option stops trading at announcement → expires_before."""
    settings = M22Settings()
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    future = _catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC))
    score, branch = _score_from_catalysts(
        catalysts=(future,),
        event_ts=event_ts,
        expiry=date(2024, 1, 25),  # SAME date as catalyst
        settings=settings,
    )
    assert score == 0.5
    assert branch == "pre_event_dte_expires_before"


def test_score_multiple_catalysts_post_event_has_priority() -> None:
    """When both post-event and pre-event match, post-event wins."""
    settings = M22Settings()
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    yesterday = _catalyst(when=datetime(2024, 1, 14, 21, 0, tzinfo=UTC))
    next_week = _catalyst(when=datetime(2024, 1, 22, 21, 0, tzinfo=UTC), kind="fda")
    score, branch = _score_from_catalysts(
        catalysts=(yesterday, next_week),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert score == 0.0
    assert branch == "post_event_blackout"


def test_score_multiple_future_catalysts_closest_wins() -> None:
    """Closest future catalyst drives the DTE comparison, not the farther one."""
    settings = M22Settings()
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    closest = _catalyst(when=datetime(2024, 1, 18, 21, 0, tzinfo=UTC))
    farther = _catalyst(when=datetime(2024, 1, 28, 21, 0, tzinfo=UTC), kind="fda")
    # Expiry Jan 22: survives closest (Jan 18) but expires before farther (Jan 28)
    score, branch = _score_from_catalysts(
        catalysts=(closest, farther),
        event_ts=event_ts,
        expiry=date(2024, 1, 22),
        settings=settings,
    )
    # Closest is Jan 18 — Jan 22 > Jan 18 → survives
    assert score == 1.0
    assert branch == "pre_event_dte_survives"


def test_score_profile_neutral_score_operator_override() -> None:
    """Operator can pin a different no_catalyst_neutral_score."""
    settings = M22Settings(no_catalyst_neutral_score=0.7)
    score, branch = _score_from_catalysts(
        catalysts=(),
        event_ts=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert score == 0.7
    assert branch == "no_catalyst"


def test_score_profile_blackout_days_operator_override() -> None:
    """Operator can pin a wider/tighter blackout window."""
    # Default 1 day; 3-day blackout → catalyst 2 days before counts
    settings = M22Settings(post_event_blackout_days=3)
    event_ts = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    two_days_ago = _catalyst(when=datetime(2024, 1, 13, 21, 0, tzinfo=UTC))
    score, branch = _score_from_catalysts(
        catalysts=(two_days_ago,),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),
        settings=settings,
    )
    assert branch == "post_event_blackout"
    assert score == 0.0
    # Same catalyst with default 1-day blackout falls through:
    # - Not in [event_date - 1, event_date] (it's 2 days ago)
    # - Not > event_date (it's in the past)
    # → neutral_fallback (telemetry distinct from "no_catalyst" because
    #   the feed DID return data, just not actionable; same 0.3 score)
    default_settings = M22Settings()  # blackout_days=1
    score2, branch2 = _score_from_catalysts(
        catalysts=(two_days_ago,),
        event_ts=event_ts,
        expiry=date(2024, 2, 16),
        settings=default_settings,
    )
    assert score2 == 0.3
    assert branch2 == "neutral_fallback"


# ---------------------------------------------------------------------------
# Stage — async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_on_preset_skips_provider() -> None:
    provider = _StubProvider(catalysts=(_catalyst(
        when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC),
    ),))
    stage = EventCalendarStage(provider=provider)
    event = _make_event(event_score=0.7)
    ctx = PipelineContext(profile=load_default_profile())
    result = await stage.enrich(event, ctx)
    assert result.event_score == 0.7  # unchanged
    assert provider.call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_timeout_emits_zero_score() -> None:
    profile = load_default_profile()
    new_m22 = profile.scoring.modules.m22.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m22": new_m22})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(
        catalysts=(_catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC)),),
        delay_seconds=0.5,
    )
    stage = EventCalendarStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.event_score == 0.0
    assert stage.last_execution_metadata == {
        "branch": "timeout",
        "provider_returned": "no",
    }


@pytest.mark.asyncio
async def test_end_to_end_no_catalyst_branch() -> None:
    provider = _StubProvider(catalysts=())  # empty
    stage = EventCalendarStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.event_score == 0.3  # no_catalyst_neutral_score
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_catalyst"
    assert stage.last_execution_metadata["provider_returned"] == "empty"
    assert stage.last_execution_metadata["catalysts_count"] == "0"


@pytest.mark.asyncio
async def test_end_to_end_pre_event_dte_survives_branch() -> None:
    provider = _StubProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC)),
    ))
    stage = EventCalendarStage(provider=provider)
    # event Jan 15, expiry Feb 16 (survives Jan 25 catalyst)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.event_score == 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "pre_event_dte_survives"
    assert stage.last_execution_metadata["provider_returned"] == "yes"
    assert stage.last_execution_metadata["catalysts_count"] == "1"


@pytest.mark.asyncio
async def test_end_to_end_post_event_blackout_branch() -> None:
    """Catalyst yesterday → post-event blackout."""
    provider = _StubProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 14, 21, 0, tzinfo=UTC)),
    ))
    stage = EventCalendarStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.event_score == 0.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "post_event_blackout"


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_provider() -> None:
    stage = EventCalendarStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp returns empty tuple → no_catalyst_neutral_score = 0.3
    assert event.event_score == 0.3
    assert isinstance(stage._provider, NoOpCatalystCalendarProvider)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_catalyst"


# ---------------------------------------------------------------------------
# Telemetry round-trip via orchestrator copy step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry

    provider = _StubProvider(catalysts=(
        _catalyst(when=datetime(2024, 1, 25, 21, 0, tzinfo=UTC)),
    ))
    stage = EventCalendarStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "pre_event_dte_survives"
    assert entry.metadata["catalysts_count"] == "1"
