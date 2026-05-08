"""Module 21 — Dealer gamma exposure score.

Phase 3.4.1: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.1.

Hypothesis (acceptance doc): when dealers are net-short gamma in a
name and spot moves toward the gamma flip point, dealer hedge
demand creates reflexive flow that amplifies the move. M21
quantifies how 'squeeze-prone' the dealer position is right now.

Provider:
  - ``DealerPositioningProvider.aggregate_for_ticker`` (extended
    in Phase 3.4.1.2) returns ``DealerExposureAggregate(net_gamma,
    flip_strike)`` per ticker.

Score branches (acceptance doc):
  - 1.0 when dealers net-short AND spot near flip
  - 0.5 when one condition met but not both
  - 0.0 when neither condition met
  - 0.0 hard cutoff when |spot - flip| / spot > extreme_distance_pct
  - None when provider returns no data (illiquid name, new IPO)

Cross-cutting acceptance pinned:
  - Provider injection via constructor (no HTTP calls inside stage)
  - Idempotency-on-preset (early return if gamma_score already set)
  - asyncio.wait_for on the provider call (timeout = profile)
  - Telemetry exposed via ``last_execution_metadata`` for the
    orchestrator to copy into ``StageExecutionEntry.metadata``

decision (None vs 0.0 score on data-missing vs timeout):
  Acceptance doc explicitly distinguishes:
    'Provider returns no data → score = None, log "no gamma data
     available", do not raise'
    'Provider timeout → score = 0.0, log warning'
  Different semantics: missing data ≠ failure; timeout IS a
  failure. None propagates through scoring's
  ``sub_score_missing_behavior`` policy; 0.0 contributes a real
  zero to the combined score.

decision (compute distance only when flip_strike is not None):
  When the provider returns flip_strike=None (curve doesn't cross
  zero), the proximity branch is unmeasurable. We collapse to the
  short-gamma-only test: if dealers are net-short, score = 0.5;
  otherwise 0.0. Pinned by tests.

decision (extreme_distance hard cutoff is a profile knob):
  Acceptance doc edge case 'distance_pct > 0.20 → score = 0.0'.
  The 0.20 lives in M21Settings.extreme_distance_pct. Test pin
  prevents a hardcoded 0.20 leaking into stage code.

decision (NoOp provider as default constructor argument):
  Existing call sites (default_stage_pipeline()) instantiate
  DealerGammaStage() with no args. The Phase 2 stub returned 0.5
  every time; the new default returns None (NoOp provider yields
  no data → score None → sub_score_missing_behavior policy
  substitutes per profile). This is a behavior change for
  call sites that don't wire a real provider, but the policy
  fallback gives them the same end-state score they'd see with
  the stub's 0.5 once Phase 3.4 calibration runs.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    NoOpDealerPositioningProvider,
)

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import M21Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.dealer_positioning import (
        DealerPositioningProvider,
    )


_logger = logging.getLogger(__name__)


class DealerGammaStage:
    """Module 21 — adds dealer-positioning context to every signal."""

    name = "m21_dealer_gamma"

    def __init__(
        self,
        provider: DealerPositioningProvider | None = None,
    ) -> None:
        """Construct with an injected provider.

        Default is the NoOp provider (returns None for every call)
        so existing pipelines that haven't wired a real provider
        get a neutral score = None and the
        sub_score_missing_behavior policy applies at scoring time.
        """
        self._provider: DealerPositioningProvider = (
            provider or NoOpDealerPositioningProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency-on-preset (acceptance rule #5)
        if event.gamma_score is not None:
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m21 = ctx.profile.scoring.modules.m21
        ticker = event.print_.ticker
        spot = event.print_.spot_price

        try:
            aggregate = await asyncio.wait_for(
                self._provider.aggregate_for_ticker(
                    ticker, event.print_.timestamp,
                ),
                timeout=m21.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m21: provider timeout for %s; emitting zero score",
                ticker,
            )
            event.gamma_score = 0.0
            self.last_execution_metadata = {
                "branch": "timeout",
                "provider_returned": "no",
            }
            return event

        if aggregate is None:
            _logger.info(
                "m21: no gamma data available for %s; score=None",
                ticker,
            )
            # event.gamma_score stays None — sub_score_missing_behavior
            # will substitute per profile policy at scoring time.
            self.last_execution_metadata = {
                "branch": "no_data",
                "provider_returned": "no",
            }
            return event

        score, branch = _score_from_aggregate(
            aggregate=aggregate, spot=spot, settings=m21,
        )
        event.gamma_score = score
        self.last_execution_metadata = {
            "branch": branch,
            "provider_returned": "yes",
        }
        return event


def _score_from_aggregate(
    *,
    aggregate: DealerExposureAggregate,
    spot: Decimal,
    settings: M21Settings,
) -> tuple[float, str]:
    """Compute the M21 score from a DealerExposureAggregate.

    Returns (score, branch_label) where branch_label is the
    score-decision branch taken (used for telemetry).

    Pure function — kept separate from the stage so it's directly
    unit-testable without async setup.
    """
    short_gamma_threshold = settings.short_gamma_threshold
    flip_proximity_pct = settings.flip_proximity_pct
    extreme_distance_pct = settings.extreme_distance_pct

    is_short_gamma = aggregate.net_gamma_dollars < short_gamma_threshold

    # Proximity computation: only meaningful when flip_strike known
    if aggregate.flip_strike is not None and spot != Decimal("0"):
        distance_pct = float(
            abs(spot - aggregate.flip_strike) / spot,
        )
    else:
        distance_pct = None

    # Hard cutoff: too far from flip to matter
    if distance_pct is not None and distance_pct > extreme_distance_pct:
        return 0.0, "extreme_distance_cutoff"

    is_proximate = (
        distance_pct is not None and distance_pct < flip_proximity_pct
    )

    if is_short_gamma and is_proximate:
        return 1.0, "full_short_and_proximate"
    if is_short_gamma or is_proximate:
        return 0.5, "partial_one_condition"
    return 0.0, "no_conditions_met"
