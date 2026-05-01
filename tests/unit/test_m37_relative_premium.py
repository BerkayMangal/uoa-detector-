"""Unit tests for ``RelativePremiumStage`` (Module 37).

Coverage:
  - All three score bands (high / mid / low) under the default v5 profile.
  - Missing-data path: provider returns None → score stays None, field
    added to ``missing_sub_scores``, structured warning emitted.
  - Boundary behavior at ``high_ratio`` and ``mid_ratio``.
  - Zero-median guard.
  - Idempotency: pre-set score is preserved.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m37_relative_premium import RelativePremiumStage
from uoa_detector.providers import (
    InMemoryMedianTradeSizeProvider,
    NoOpMedianTradeSizeProvider,
)


def _event(*, ticker: str = "AAPL", premium: str = "4500") -> EnrichedEvent:
    """Build an EnrichedEvent with the print's premium overridable."""
    return EnrichedEvent(
        print=OptionsPrint(
            event_id="test-1",
            timestamp=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
            ticker=ticker,
            option_type="call",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal(premium),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=False,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )


def _ctx_with(median: Decimal | None) -> PipelineContext:
    """PipelineContext where the median provider returns ``median`` for AAPL."""
    if median is None:
        return PipelineContext(
            profile=load_default_profile(),
            median_trade_size_provider=NoOpMedianTradeSizeProvider(),
        )
    return PipelineContext(
        profile=load_default_profile(),
        median_trade_size_provider=InMemoryMedianTradeSizeProvider(
            {"AAPL": median},
        ),
    )


# ---------------------------------------------------------------------------
# Band selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ratio_above_high_yields_score_high() -> None:
    """premium=4500, median=1000 → ratio=4.5 → ≥ high_ratio (3.0) → 1.0."""
    event = _event(premium="4500")
    ctx = _ctx_with(Decimal("1000"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == ctx.profile.relative_premium.score_high
    assert out.relative_premium_score == 1.0


@pytest.mark.asyncio
async def test_ratio_in_mid_band_yields_score_mid() -> None:
    """premium=2000, median=1000 → ratio=2.0 → in [1.5, 3.0) → 0.5."""
    event = _event(premium="2000")
    ctx = _ctx_with(Decimal("1000"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == ctx.profile.relative_premium.score_mid
    assert out.relative_premium_score == 0.5


@pytest.mark.asyncio
async def test_ratio_below_mid_yields_score_low() -> None:
    """premium=1000, median=1000 → ratio=1.0 → < 1.5 → 0.0."""
    event = _event(premium="1000")
    ctx = _ctx_with(Decimal("1000"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == ctx.profile.relative_premium.score_low
    assert out.relative_premium_score == 0.0


# ---------------------------------------------------------------------------
# Boundary behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ratio_exactly_high_ratio_is_inclusive_high() -> None:
    """ratio = high_ratio (3.0) → score_high (>= comparison)."""
    event = _event(premium="3000")
    ctx = _ctx_with(Decimal("1000"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == ctx.profile.relative_premium.score_high


@pytest.mark.asyncio
async def test_ratio_exactly_mid_ratio_is_inclusive_mid() -> None:
    """ratio = mid_ratio (1.5) → score_mid (>= comparison)."""
    event = _event(premium="1500")
    ctx = _ctx_with(Decimal("1000"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == ctx.profile.relative_premium.score_mid


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_median_records_in_missing_sub_scores_and_logs() -> None:
    event = _event()
    ctx = _ctx_with(None)  # NoOpProvider — returns None for everything

    with capture_logs() as cap:
        out = await RelativePremiumStage().enrich(event, ctx)

    assert out.relative_premium_score is None
    assert "relative_premium_score" in out.missing_sub_scores

    warnings = [r for r in cap if r.get("event") == "relative_premium_no_median"]
    assert warnings, f"expected relative_premium_no_median warning; got {cap}"
    assert warnings[0]["ticker"] == "AAPL"
    assert warnings[0]["window_days"] == ctx.profile.relative_premium.median_window_days


@pytest.mark.asyncio
async def test_missing_median_does_not_double_record() -> None:
    """Stage is idempotent on the missing-data path too."""
    event = _event()
    ctx = _ctx_with(None)
    stage = RelativePremiumStage()
    await stage.enrich(event, ctx)
    await stage.enrich(event, ctx)
    # Only one entry, even though stage ran twice.
    assert event.missing_sub_scores.count("relative_premium_score") == 1


@pytest.mark.asyncio
async def test_zero_median_is_treated_as_missing() -> None:
    """A 0 median would make the ratio undefined; treat same as None."""
    event = _event()
    ctx = _ctx_with(Decimal("0"))
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score is None
    assert "relative_premium_score" in out.missing_sub_scores


# ---------------------------------------------------------------------------
# Idempotency: pre-set score is preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_set_score_is_not_overwritten() -> None:
    """Scenario override stage may set a score; M37 must not clobber it."""
    event = _event()
    event.relative_premium_score = 0.7  # pre-set, e.g. by override stage
    ctx = _ctx_with(Decimal("1000"))  # would normally produce 1.0 (4500/1000=4.5)
    out = await RelativePremiumStage().enrich(event, ctx)
    assert out.relative_premium_score == 0.7  # untouched


@pytest.mark.asyncio
async def test_pre_set_score_does_not_call_provider() -> None:
    """Idempotent path skips the provider entirely (efficiency + side-effect free)."""

    class TrackingProvider:
        """Records every call so we can assert it wasn't invoked."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def median_premium(
            self, ticker: str, window_days: int,
        ) -> Decimal | None:
            self.calls.append((ticker, window_days))
            return Decimal("1000")

    tracker = TrackingProvider()
    event = _event()
    event.relative_premium_score = 0.42  # pre-set
    ctx = PipelineContext(
        profile=load_default_profile(),
        median_trade_size_provider=tracker,  # type: ignore[arg-type]
    )
    await RelativePremiumStage().enrich(event, ctx)
    assert tracker.calls == []  # provider was never invoked
