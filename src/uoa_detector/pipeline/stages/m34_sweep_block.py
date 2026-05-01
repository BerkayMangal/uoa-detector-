"""Module 34 — Sweep vs Block Order Classifier.

Classifies each canonical OptionsPrint into one of three categories and
emits the corresponding bonus to ``uoa_score`` as a ``ScoreAdjustment``
(non-mutating, audit-trail friendly).

Classification (mutually exclusive):

  - ``"iso"`` — the print's source(s) tagged it as Intermarket Sweep Order.
    A direct urgency signal: the trader bypassed the NBBO mechanism to fill
    immediately. Bonus = ``profile.sweep.iso_bonus``.

  - ``"sweep"`` — multi-venue execution within the cross-venue window:
    ``len(source_agreement.exchanges_seen) >= 2`` AND
    ``source_agreement.timestamp_skew_ms <= profile.sweep.cross_venue_window_ms``.
    Sub-classified by fill side:
      * ``fill_side == "above_ask"`` → ``cross_venue_above_ask_bonus`` (the
        most aggressive flavour — paying through the NBBO across multiple
        venues simultaneously).
      * otherwise → ``cross_venue_bonus``.

  - ``"block"`` — anything else. No bonus, single-venue or stale-skew flow
    that doesn't qualify as sweep.

**Bonuses do NOT stack.** Per the Phase 2.3.2 design decision: a print is
exactly one of the three categories. ISO outranks sweep (ISO is itself a
form of multi-venue execution, just explicitly tagged); above-ask sweep
outranks plain sweep. The precedence is implemented as a flat
if/elif/elif/else so the classification is obvious at a glance.

Idempotency: if the override stage pre-set ``sweep_classification``, this
stage trusts it. If pre-set, the stage still emits the matching bonus
adjustment (so test scenarios that hand-pin ``sweep_classification`` still
get the correct boost). The ``score_adjustments`` list is checked to
avoid double-emission on a second run.

UOA score initialization: if ``uoa_score`` is None on entry (no upstream
stage has set it), this stage sets it to a neutral 0.5 baseline before
any adjustments. The Phase 1 stub did the same; preserved for consistency
with downstream scoring.
"""

from __future__ import annotations

from uoa_detector.domain.agreement import ScoreAdjustment
from uoa_detector.domain.events import EnrichedEvent, SweepClassification
from uoa_detector.pipeline.stage import PipelineContext

_MODULE_NAME = "m34_sweep_block"


class SweepBlockStage:
    """Module 34 — classify each print and emit the matching uoa_score bonus."""

    name = _MODULE_NAME

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        params = ctx.profile.sweep
        sa = event.print_.source_agreement

        # Determine classification. Override-stage may have pre-set one;
        # trust it if so (test scenarios pin classifications directly).
        classification: SweepClassification
        if event.sweep_classification is not None:
            classification = event.sweep_classification
        elif event.print_.is_iso:
            classification = "iso"
        elif (
            len(sa.exchanges_seen) >= 2
            and sa.timestamp_skew_ms <= params.cross_venue_window_ms
        ):
            classification = "sweep"
        else:
            classification = "block"

        event.sweep_classification = classification

        # Resolve the matching bonus per non-stacking precedence:
        #   iso             → iso_bonus
        #   sweep+above_ask → cross_venue_above_ask_bonus
        #   sweep           → cross_venue_bonus
        #   block           → no bonus
        bonus_delta: float
        bonus_reason: str
        if classification == "iso":
            bonus_delta = params.iso_bonus
            bonus_reason = "ISO sweep — direct urgency tag from source"
        elif classification == "sweep" and event.print_.fill_side == "above_ask":
            bonus_delta = params.cross_venue_above_ask_bonus
            bonus_reason = (
                "cross-venue sweep + above-ask fill — most aggressive execution"
            )
        elif classification == "sweep":
            bonus_delta = params.cross_venue_bonus
            bonus_reason = "cross-venue sweep within window"
        else:
            # block: no bonus to emit. Ensure uoa_score has a baseline.
            if event.uoa_score is None:
                event.uoa_score = 0.5
            return event

        # Set the uoa_score baseline if not already set, then emit one
        # adjustment. Idempotency: don't re-add if this module already emitted
        # one for this event.
        if event.uoa_score is None:
            event.uoa_score = 0.5

        already_emitted = any(
            adj.source_module == _MODULE_NAME and adj.target == "uoa_score"
            for adj in event.score_adjustments
        )
        if not already_emitted:
            event.score_adjustments.append(
                ScoreAdjustment(
                    target="uoa_score",
                    delta=bonus_delta,
                    reason=bonus_reason,
                    source_module=_MODULE_NAME,
                ),
            )

        return event
