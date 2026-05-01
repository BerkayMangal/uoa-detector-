"""Module 37 — Relative Premium Sizing Filter.

Replaces v4's binary "large premium" flag with a banded score that reflects
how much a print stands out vs the ticker's typical trade size:

    ratio = premium_paid / ticker_30d_median_trade_size_in_usd

  - ratio ≥ profile.relative_premium.high_ratio (default 3.0) → score_high (1.0)
  - ratio ≥ profile.relative_premium.mid_ratio (default 1.5) → score_mid (0.5)
  - otherwise                                                → score_low (0.0)

The denominator is reference data (slow-changing, ticker-specific). It comes
from a ``MedianTradeSizeProvider`` injected via ``PipelineContext``. In
production the provider hits IBKR / Polygon snapshots; in tests it's an
``InMemoryMedianTradeSizeProvider`` driven by a dict; in the synthetic CLI
mode it's a ``CSVMedianTradeSizeProvider`` reading ``data/medians.csv``.

Idempotency: if ``event.relative_premium_score`` is already set (e.g., by
the ``ScenarioOverrideStage`` that runs ahead of this stage in the
synthetic CLI scenarios), the stage is a no-op. This is what lets
hand-authored test scenarios pin a specific score without rebuilding the
provider chain.

Missing-data handling: if the provider returns ``None`` for the ticker,
``relative_premium_score`` stays ``None`` and the stage adds the field name
to ``event.missing_sub_scores`` so the scoring engine's
``sub_score_missing_behavior`` policy can pick it up. A structured warning
is emitted with ``ticker`` and the ``window_days`` requested so the operator
can see which ticker is missing reference data.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.pipeline.stage import PipelineContext

_logger = structlog.get_logger(__name__)


class RelativePremiumStage:
    """Module 37 — score the print's premium against the ticker's median."""

    name = "m37_relative_premium"

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency: do not overwrite a score already populated upstream
        # (typically the scenario override stage). This is consistent with
        # how the Phase 1 stub behaved.
        if event.relative_premium_score is not None:
            return event

        params = ctx.profile.relative_premium
        median = await ctx.median_trade_size_provider.median_premium(
            event.print_.ticker,
            params.median_window_days,
        )

        if median is None or median == Decimal(0):
            # Provider returned no data for this ticker (or returned 0,
            # which would make the ratio undefined). Record the gap so
            # the scoring engine's missing-policy can act on it.
            _logger.warning(
                "relative_premium_no_median",
                ticker=event.print_.ticker,
                window_days=params.median_window_days,
            )
            if "relative_premium_score" not in event.missing_sub_scores:
                event.missing_sub_scores.append("relative_premium_score")
            return event

        ratio = float(event.print_.premium_paid / median)

        if ratio >= params.high_ratio:
            event.relative_premium_score = params.score_high
        elif ratio >= params.mid_ratio:
            event.relative_premium_score = params.score_mid
        else:
            event.relative_premium_score = params.score_low

        return event
