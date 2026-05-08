"""Phase 3.4.7.2 tests for ``OpeningClosingStage`` (Module 27).

Pins (acceptance doc §3.4.7):
  Pure _score_from_oi_snapshots function:
    - current None → no_current_data (0.5)
    - prior None → no_prior_data (0.5; not new_strike)
    - prior == 0 → new_strike (1.0)
    - delta > 0.5 → strong_opening (1.0)
    - delta in (0.1, 0.5] → moderate_opening (0.7)
    - delta in [-0.1, 0.1] → neutral (0.5)
    - delta < -0.1 → closing (0.0)
    - exact threshold boundaries map correctly
    - profile threshold operator override changes branch

  Pure _compute_prior_session_close function:
    - returns yesterday at session_close_hour:minute UTC
    - independent of event_ts time-of-day

  Stage async:
    - Provider timeout → branch=timeout, no score
    - Both prior + current None → no_current_data branch
    - Strong opening end-to-end
    - Closing end-to-end
    - New strike end-to-end (prior_oi=0)
    - Stale OI warning fires when prior == current
    - Default constructor uses NoOp provider

  Telemetry:
    - last_execution_metadata round-trips into StageExecutionEntry
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M27Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m27_opening_closing import (
    OpeningClosingStage,
    _compute_prior_session_close,
    _score_from_oi_snapshots,
)
from uoa_detector.providers.open_interest import (
    NoOpOpenInterestProvider,
    OpenInterestSnapshot,
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


def _snapshot(
    open_interest: int,
    when: datetime | None = None,
) -> OpenInterestSnapshot:
    if when is None:
        when = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    return OpenInterestSnapshot(
        ticker="AAPL",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        option_type="call",
        as_of=when,
        open_interest=open_interest,
    )


class _StubProvider:
    """Configurable OpenInterestProvider for M27 tests.

    Returns ``current`` snapshot when ``when >= cutoff`` (event_ts),
    else returns ``prior``. Cutoff defaults to a moment past the
    test event_ts so the second call returns current.
    """

    def __init__(
        self,
        *,
        prior: OpenInterestSnapshot | None,
        current: OpenInterestSnapshot | None,
        cutoff: datetime | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._prior = prior
        self._current = current
        self._cutoff = cutoff or datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        self._delay = delay_seconds
        self.at_call_count = 0

    async def at(  # type: ignore[no-untyped-def]
        self, ticker, strike, expiry, option_type, when,
    ):
        self.at_call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._current if when >= self._cutoff else self._prior

    async def next_day(  # type: ignore[no-untyped-def]
        self, ticker, strike, expiry, option_type, trade_date,
    ):
        return None


# ---------------------------------------------------------------------------
# Pure _compute_prior_session_close
# ---------------------------------------------------------------------------


def test_prior_session_close_returns_yesterday_at_close_hour() -> None:
    """Mid-day Mon → Sun at session close (no holiday handling)."""
    event = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)  # Mon 15:30 UTC
    out = _compute_prior_session_close(
        event_ts=event,
        session_close_hour=21,
        session_close_minute=0,
    )
    assert out == datetime(2024, 1, 14, 21, 0, tzinfo=UTC)


def test_prior_session_close_independent_of_event_time_of_day() -> None:
    """Whether event is 09:00 or 23:00, prior_close is yesterday-21:00."""
    early = datetime(2024, 1, 15, 9, 0, tzinfo=UTC)
    late = datetime(2024, 1, 15, 23, 0, tzinfo=UTC)
    out_early = _compute_prior_session_close(
        event_ts=early, session_close_hour=21, session_close_minute=0,
    )
    out_late = _compute_prior_session_close(
        event_ts=late, session_close_hour=21, session_close_minute=0,
    )
    assert out_early == out_late
    assert out_early == datetime(2024, 1, 14, 21, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure _score_from_oi_snapshots
# ---------------------------------------------------------------------------


def test_score_current_none_no_current_data() -> None:
    s = M27Settings()
    score, branch, delta, prior_oi, current_oi = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=None, settings=s,
    )
    assert score == 0.5
    assert branch == "no_current_data"
    assert delta is None
    assert prior_oi == 1000
    assert current_oi is None


def test_score_prior_none_no_prior_data_not_new_strike() -> None:
    """Defensive: prior=None means data gap, NOT new strike (1.0)."""
    s = M27Settings()
    score, branch, _, prior_oi, current_oi = _score_from_oi_snapshots(
        prior=None, current=_snapshot(500), settings=s,
    )
    assert score == 0.5
    assert branch == "no_prior_data"
    assert prior_oi is None
    assert current_oi == 500


def test_score_prior_zero_new_strike() -> None:
    s = M27Settings()
    score, branch, delta, prior_oi, _ = _score_from_oi_snapshots(
        prior=_snapshot(0), current=_snapshot(500), settings=s,
    )
    assert score == 1.0
    assert branch == "new_strike"
    assert delta is None  # division skipped
    assert prior_oi == 0


def test_score_strong_opening_branch() -> None:
    """delta > 0.5 → strong_opening."""
    s = M27Settings()
    # 1000 → 1600 = +60% delta
    score, branch, delta, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(1600), settings=s,
    )
    assert score == 1.0
    assert branch == "strong_opening"
    assert delta is not None
    assert delta == pytest.approx(0.6, rel=1e-6)


def test_score_moderate_opening_branch() -> None:
    """delta in (0.1, 0.5] → moderate_opening."""
    s = M27Settings()
    # 1000 → 1300 = +30% delta
    score, branch, delta, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(1300), settings=s,
    )
    assert score == 0.7
    assert branch == "moderate_opening"
    assert delta == pytest.approx(0.3, rel=1e-6)


def test_score_neutral_branch_small_increase() -> None:
    s = M27Settings()
    # 1000 → 1050 = +5% delta
    score, branch, _, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(1050), settings=s,
    )
    assert score == 0.5
    assert branch == "neutral"


def test_score_neutral_branch_small_decrease() -> None:
    s = M27Settings()
    # 1000 → 950 = -5% delta
    score, branch, _, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(950), settings=s,
    )
    assert score == 0.5
    assert branch == "neutral"


def test_score_closing_branch() -> None:
    """delta < -0.1 → closing."""
    s = M27Settings()
    # 1000 → 700 = -30% delta
    score, branch, delta, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(700), settings=s,
    )
    assert score == 0.0
    assert branch == "closing"
    assert delta == pytest.approx(-0.3, rel=1e-6)


def test_score_exact_threshold_boundaries() -> None:
    """Exact threshold values map to the lower-score branch.

    Strict-greater for thresholds means:
      - delta == 0.5 (exactly strong threshold) → moderate
        (not strong)
      - delta == 0.1 (exactly moderate threshold) → neutral
        (not moderate)
      - delta == -0.1 (exactly closing threshold) → neutral
        (not closing; closing requires < -0.1)
    """
    s = M27Settings()
    # delta = 0.5 exactly: 1000 → 1500
    _score, branch, _, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(1500), settings=s,
    )
    assert branch == "moderate_opening"  # 0.5 not > 0.5
    # delta = 0.1 exactly: 1000 → 1100
    _score, branch, _, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(1100), settings=s,
    )
    assert branch == "neutral"  # 0.1 not > 0.1
    # delta = -0.1 exactly: 1000 → 900
    _score, branch, _, _, _ = _score_from_oi_snapshots(
        prior=_snapshot(1000), current=_snapshot(900), settings=s,
    )
    assert branch == "neutral"  # -0.1 not < -0.1


def test_score_operator_can_override_thresholds() -> None:
    """Tighter operator strong threshold reclassifies a borderline delta."""
    # +30% delta
    prior_s = _snapshot(1000)
    current_s = _snapshot(1300)
    # Default: 0.3 in (0.1, 0.5] → moderate
    _score, branch_def, _, _, _ = _score_from_oi_snapshots(
        prior=prior_s, current=current_s, settings=M27Settings(),
    )
    assert branch_def == "moderate_opening"
    # Operator tighter strong=0.2 → 0.3 > 0.2 → strong
    _score, branch_op, _, _, _ = _score_from_oi_snapshots(
        prior=prior_s, current=current_s,
        settings=M27Settings(strong_opening_threshold=0.2),
    )
    assert branch_op == "strong_opening"


# ---------------------------------------------------------------------------
# Stage async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_timeout_emits_neutral() -> None:
    profile = load_default_profile()
    new_m27 = profile.scoring.modules.m27.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m27": new_m27})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(1500),
        delay_seconds=0.5,
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata == {
        "branch": "timeout",
        "provider_returned": "no",
    }


@pytest.mark.asyncio
async def test_both_none_emits_no_current_data() -> None:
    """Provider returns None for both → no_current_data branch."""
    provider = _StubProvider(prior=None, current=None)
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_current_data"
    assert stage.last_execution_metadata["provider_returned"] == "no"


@pytest.mark.asyncio
async def test_end_to_end_strong_opening() -> None:
    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(2000),  # +100%
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "strong_opening"
    assert stage.last_execution_metadata["prior_oi"] == "1000"
    assert stage.last_execution_metadata["current_oi"] == "2000"
    assert stage.last_execution_metadata["oi_delta_pct"] == "1.000000"


@pytest.mark.asyncio
async def test_end_to_end_closing() -> None:
    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(700),  # -30%
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "closing"


@pytest.mark.asyncio
async def test_end_to_end_new_strike() -> None:
    """Prior OI = 0 → new_strike branch."""
    provider = _StubProvider(
        prior=_snapshot(0), current=_snapshot(500),
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "new_strike"
    assert stage.last_execution_metadata["prior_oi"] == "0"
    assert stage.last_execution_metadata["oi_delta_pct"] == "n/a"


@pytest.mark.asyncio
async def test_stale_warning_fires_when_oi_unchanged() -> None:
    """prior_oi == current_oi (and > 0) → stale warning + neutral score."""
    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(1000),  # delta = 0
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "neutral"
    assert stage.last_execution_metadata["stale_warning_fired"] == "yes"


@pytest.mark.asyncio
async def test_stale_warning_does_not_fire_on_actual_change() -> None:
    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(1050),  # +5%
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["stale_warning_fired"] == "no"


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_provider() -> None:
    stage = OpeningClosingStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp returns None for both → no_current_data
    assert isinstance(stage._provider, NoOpOpenInterestProvider)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_current_data"


# ---------------------------------------------------------------------------
# Telemetry round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry
    provider = _StubProvider(
        prior=_snapshot(1000), current=_snapshot(1300),
    )
    stage = OpeningClosingStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "moderate_opening"
    assert entry.metadata["prior_oi"] == "1000"
    assert entry.metadata["current_oi"] == "1300"
    assert entry.metadata["oi_delta_pct"] == "0.300000"
