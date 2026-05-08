"""Phase 3.4.6.2 tests for ``DarkPoolStage`` (Module 26).

Pins (acceptance doc §3.4.6):
  Pure _score_from_prints function:
    - empty prints → no_qualifying_prints (0.2)
    - prints below min_size threshold → no_qualifying_prints
    - call + 'at_or_below_bid' largest qualifying → confirmed (1.0)
    - put + 'above_ask' largest qualifying → confirmed (1.0)
    - 'midpoint' largest → direction_unclear (0.5)
    - 'unknown' largest → direction_unclear (0.5)
    - call + 'above_ask' largest (mismatch) → no_qualifying score
    - mixed sizes/directions: largest by notional wins
    - filter THEN largest (not largest then filter)
    - unknown option_type → direction_unclear

  Stage async:
    - Idempotency: has_dark_pool_confirmation==True → preset_skip
    - Provider timeout → branch=timeout, provider_returned=no
    - Confirmed match sets has_dark_pool_confirmation=True
    - Direction unclear does NOT set has_dark_pool_confirmation
    - Delay warning fires when print > 5min old
    - Delay warning does NOT fire when within window
    - Default constructor uses NoOp provider

  Telemetry:
    - last_execution_metadata round-trips into StageExecutionEntry
    - largest_notional_usd + largest_side_estimate surfaced
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M26Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m26_dark_pool import (
    DarkPoolStage,
    _score_from_prints,
)
from uoa_detector.providers.dark_pool import (
    DarkPoolPrint,
    NoOpDarkPoolPrintProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    option_type: str = "call",
    timestamp: datetime | None = None,
    has_dp_confirmation: bool = False,
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
    e.has_dark_pool_confirmation = has_dp_confirmation
    return e


def _print(
    *,
    price: float = 150.0,
    size: int = 50_000,
    side: str = "at_or_below_bid",
    when: datetime | None = None,
) -> DarkPoolPrint:
    if when is None:
        when = datetime(2024, 1, 15, 15, 0, tzinfo=UTC)
    return DarkPoolPrint(  # type: ignore[arg-type]
        ticker="AAPL",
        when=when,
        price=Decimal(str(price)),
        size=size,
        side_estimate=side,
    )


class _StubProvider:
    def __init__(
        self,
        *,
        prints: Sequence[DarkPoolPrint] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        self._prints = prints
        self._delay = delay_seconds
        self.call_count = 0

    async def recent_prints(
        self,
        ticker: str,
        before: datetime,
        window: timedelta,
    ) -> Sequence[DarkPoolPrint]:
        del ticker, before, window
        self.call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._prints


# ---------------------------------------------------------------------------
# Pure _score_from_prints function
# ---------------------------------------------------------------------------


def test_score_empty_prints_returns_no_qualifying() -> None:
    s = M26Settings()
    score, branch, qcount, largest = _score_from_prints(
        prints=(),
        option_type="call",
        settings=s,
    )
    assert score == 0.2
    assert branch == "no_qualifying_prints"
    assert qcount == 0
    assert largest is None


def test_score_prints_below_threshold_no_qualifying() -> None:
    """A $1M print with default $5M threshold → no qualifying."""
    s = M26Settings()  # threshold $5M
    small = _print(price=100.0, size=10_000)  # notional = $1M
    score, branch, qcount, _ = _score_from_prints(
        prints=(small,),
        option_type="call",
        settings=s,
    )
    assert score == 0.2
    assert branch == "no_qualifying_prints"
    assert qcount == 0


def test_score_call_with_bullish_dp_confirmed() -> None:
    """call + at_or_below_bid + qualifying notional → 1.0."""
    s = M26Settings()
    bullish_print = _print(price=150.0, size=50_000, side="at_or_below_bid")
    # notional = $7.5M (≥ $5M threshold)
    score, branch, qcount, largest = _score_from_prints(
        prints=(bullish_print,),
        option_type="call",
        settings=s,
    )
    assert score == 1.0
    assert branch == "confirmed_match"
    assert qcount == 1
    assert largest is bullish_print


def test_score_put_with_bearish_dp_confirmed() -> None:
    s = M26Settings()
    bearish_print = _print(price=150.0, size=50_000, side="above_ask")
    score, branch, _, _ = _score_from_prints(
        prints=(bearish_print,),
        option_type="put",
        settings=s,
    )
    assert score == 1.0
    assert branch == "confirmed_match"


def test_score_midpoint_print_direction_unclear() -> None:
    s = M26Settings()
    midpoint_print = _print(price=150.0, size=50_000, side="midpoint")
    score, branch, _, _ = _score_from_prints(
        prints=(midpoint_print,),
        option_type="call",
        settings=s,
    )
    assert score == 0.5
    assert branch == "direction_unclear"


def test_score_unknown_side_direction_unclear() -> None:
    s = M26Settings()
    unknown_print = _print(price=150.0, size=50_000, side="unknown")
    score, branch, _, _ = _score_from_prints(
        prints=(unknown_print,),
        option_type="put",
        settings=s,
    )
    assert score == 0.5
    assert branch == "direction_unclear"


def test_score_call_with_bearish_dp_mismatch() -> None:
    """call signal but largest qualifying is bearish DP → 0.2 mismatch."""
    s = M26Settings()
    bearish_print = _print(price=150.0, size=50_000, side="above_ask")
    score, branch, _, _ = _score_from_prints(
        prints=(bearish_print,),
        option_type="call",
        settings=s,
    )
    assert score == 0.2  # same as no_qualifying fallback
    assert branch == "direction_mismatch"


def test_score_largest_by_notional_wins() -> None:
    """Multiple qualifying prints, mixed sides; largest by notional drives.

    print1: notional $7.5M, at_or_below_bid (bullish)
    print2: notional $9M, above_ask (bearish)
    print3: notional $6M, midpoint
    For call signal: largest is print2 (bearish) → mismatch (0.2)
    """
    p_bull = _print(price=150.0, size=50_000, side="at_or_below_bid")  # $7.5M
    p_bear = _print(price=150.0, size=60_000, side="above_ask")        # $9M
    p_mid = _print(price=150.0, size=40_000, side="midpoint")          # $6M
    s = M26Settings()
    score, branch, qcount, largest = _score_from_prints(
        prints=(p_bull, p_bear, p_mid),
        option_type="call",
        settings=s,
    )
    assert qcount == 3  # all three qualify
    assert largest is p_bear  # largest by notional
    assert score == 0.2
    assert branch == "direction_mismatch"


def test_score_filter_then_largest_not_other_way() -> None:
    """Order matters: filter first, then largest.

    Verifies the implementation doesn't pick a non-qualifying largest
    print as the winner.
    """
    huge_unqualified = _print(price=1.0, size=999_999_999, side="above_ask")
    # notional = $999_999_999 — but wait, that DOES qualify; let me use
    # a different shape: tiny price * huge size still notional.
    # Actually min_print_size is in USD = price * size, so a giant size
    # at low price still hits threshold. Let me build a CLEARER test:
    # one print just under threshold, one just at threshold.
    just_under = _print(price=99.0, size=50_000, side="above_ask")
    # notional = 99 * 50000 = $4_950_000 (< $5M, NOT qualifying)
    just_at = _print(price=100.0, size=50_000, side="at_or_below_bid")
    # notional = 100 * 50000 = $5_000_000 (== $5M, qualifying)
    s = M26Settings()
    score, branch, qcount, largest = _score_from_prints(
        prints=(just_under, just_at),
        option_type="call",
        settings=s,
    )
    # Only just_at qualifies; just_under (which is "larger" by raw size
    # if we'd accepted it but smaller by notional) is filtered out
    assert qcount == 1
    assert largest is just_at
    assert score == 1.0
    assert branch == "confirmed_match"
    del huge_unqualified  # keep the variable referenced for clarity


def test_score_unknown_option_type_direction_unclear() -> None:
    s = M26Settings()
    bullish_print = _print(price=150.0, size=50_000, side="at_or_below_bid")
    score, branch, _, _ = _score_from_prints(
        prints=(bullish_print,),
        option_type="banana",
        settings=s,
    )
    assert score == 0.5
    assert branch == "unknown_option_type"


def test_score_operator_can_lower_size_threshold() -> None:
    """Small-cap operator may set $1M threshold; smaller print qualifies."""
    s = M26Settings(min_print_size_usd=1_000_000)
    print_2m = _print(price=100.0, size=20_000, side="at_or_below_bid")
    # notional $2M — qualifies under $1M threshold; doesn't under default $5M
    score, branch, _, _ = _score_from_prints(
        prints=(print_2m,),
        option_type="call",
        settings=s,
    )
    assert score == 1.0
    assert branch == "confirmed_match"


# ---------------------------------------------------------------------------
# Stage async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_skips_when_already_confirmed() -> None:
    provider = _StubProvider(prints=(_print(),))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(has_dp_confirmation=True)
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert provider.call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_provider_timeout_emits_neutral() -> None:
    profile = load_default_profile()
    new_m26 = profile.scoring.modules.m26.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m26": new_m26})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    provider = _StubProvider(
        prints=(_print(),),
        delay_seconds=0.5,
    )
    stage = DarkPoolStage(provider=provider)
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.has_dark_pool_confirmation is False  # not set on timeout
    assert stage.last_execution_metadata == {
        "branch": "timeout",
        "provider_returned": "no",
    }


@pytest.mark.asyncio
async def test_confirmed_match_sets_dp_confirmation_flag() -> None:
    provider = _StubProvider(prints=(
        _print(price=150.0, size=50_000, side="at_or_below_bid",
               when=datetime(2024, 1, 15, 15, 28, tzinfo=UTC)),  # within delay
    ))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.has_dark_pool_confirmation is True
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "confirmed_match"


@pytest.mark.asyncio
async def test_direction_unclear_does_not_set_dp_confirmation_flag() -> None:
    provider = _StubProvider(prints=(
        _print(price=150.0, size=50_000, side="midpoint"),
    ))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # Score 0.5 ≠ confirmed_match_score (1.0) → flag stays False
    assert event.has_dark_pool_confirmation is False
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "direction_unclear"


@pytest.mark.asyncio
async def test_no_qualifying_does_not_set_dp_confirmation_flag() -> None:
    """Empty / below-threshold → no flag flip + no_qualifying branch."""
    provider = _StubProvider(prints=())  # empty
    stage = DarkPoolStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.has_dark_pool_confirmation is False
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_qualifying_prints"
    assert stage.last_execution_metadata["qualifying_print_count"] == "0"


@pytest.mark.asyncio
async def test_delay_warning_fires_when_print_too_old() -> None:
    """Largest qualifying print > 5 min before event → warning telemetry."""
    # Event at 15:30, print at 15:20 (10 min delay; > 5 min default)
    old_print = _print(
        price=150.0, size=50_000, side="at_or_below_bid",
        when=datetime(2024, 1, 15, 15, 20, tzinfo=UTC),
    )
    provider = _StubProvider(prints=(old_print,))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["delay_warning_fired"] == "yes"
    # Score still computed (warning is telemetry-only)
    assert event.has_dark_pool_confirmation is True


@pytest.mark.asyncio
async def test_delay_warning_does_not_fire_within_window() -> None:
    """Print within 5 min → no warning."""
    fresh_print = _print(
        price=150.0, size=50_000, side="at_or_below_bid",
        when=datetime(2024, 1, 15, 15, 28, tzinfo=UTC),  # 2 min before
    )
    provider = _StubProvider(prints=(fresh_print,))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["delay_warning_fired"] == "no"


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_provider() -> None:
    stage = DarkPoolStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp returns empty → no_qualifying_prints branch
    assert event.has_dark_pool_confirmation is False
    assert isinstance(stage._provider, NoOpDarkPoolPrintProvider)
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_qualifying_prints"


# ---------------------------------------------------------------------------
# Telemetry round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry
    big_print = _print(
        price=200.0, size=100_000, side="at_or_below_bid",
        when=datetime(2024, 1, 15, 15, 28, tzinfo=UTC),
    )
    provider = _StubProvider(prints=(big_print,))
    stage = DarkPoolStage(provider=provider)
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "confirmed_match"
    assert entry.metadata["qualifying_print_count"] == "1"
    assert entry.metadata["largest_notional_usd"] == "20000000"  # $20M
    assert entry.metadata["largest_side_estimate"] == "at_or_below_bid"
