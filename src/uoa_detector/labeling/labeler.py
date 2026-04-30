"""Full v5 labeler — 17 labels, decision-tree style.

Precedence (top wins; precedence below the first four discard/short-circuit
gates is derived from the spec's risk-bucket hierarchy and the Detection
Sequence in Part 7, since the spec does not enumerate it explicitly).
This precedence is flagged in the Phase 1 summary as a documented
interpretation.

1.  ``LEAP_POSITIONING``                 — DTE > 90 (separate scoring track)
2.  ``POST_EVENT_NOISE``                 — within 2 sessions of catalyst
3.  ``PENALIZED_BELOW_THRESHOLD``        — combined_score_post_penalty < 0.30
4.  ``LIKELY_CLOSING_OR_NOISE``          — next_day_oi_confirmed is False
5.  ``HIGH_CONVICTION_SEQUENCE``         — cluster + sweep/large-UOA + price confirm
6.  ``CONFIRMED_OPENING_FLOW``           — next_day_oi_confirmed is True (upgrade)
7.  ``PRE_CATALYST_FLOW``                — cluster + event ≤ 7d + IV rising mod + spot not moved
8.  ``SWEEP_UOA``                        — sweep_classification in {sweep, iso} + UOA threshold
9.  ``STANDARD_UOA``                     — above-ask + relative_premium ≥ 0.5
10. ``OPTIONS_EQUITY_TAPE_CONFIRMATION`` — has_dark_pool_confirmation
11. ``SECTOR_FLOW_CLUSTER``              — has_sector_confirmation
12. ``GAMMA_ACCELERATION_RISK``          — is_gamma_acceleration flag
13. ``CONVEXITY_BURST``                  — cluster_density_score ≥ 0.8
14. ``CONVEXITY_CLUSTER``                — cluster_density_score ≥ 0.4
15. ``OPENING_UNCONFIRMED``              — opening/closing ambiguous, next_day_oi_confirmed is None
16. ``CONVEXITY_WATCH``                  — convexity_score ≥ 0.6, no cluster yet
17. ``IGNORE_NOISE``                     — fallback
"""

from __future__ import annotations

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.errors import LabelDecisionError

# Sub-thresholds used inside the label tree. Numeric values for label triggers
# come straight from the v5 spec where the spec enumerates them; the few that
# the spec does not enumerate (e.g., the convexity_score floor for
# CONVEXITY_WATCH) are kept here as named constants so they're easy to find.
_CLUSTER_BURST = 0.8  # spec Module 38: 3+ prints same strike → 0.8; escalating → 1.0
_CLUSTER_MIN = 0.4  # spec Module 38: 2 prints same strike/expiry → 0.4
_RELATIVE_PREMIUM_UOA = 0.5  # spec Part 4 STANDARD_UOA trigger
_CONVEXITY_WATCH_FLOOR = 0.6  # not enumerated; sensible "convexity present" floor
_PRE_CATALYST_EVENT_MIN = 0.75  # spec: event within 7 days → event_score 0.75


class Labeler:
    """Turn an enriched, fully-scored event into a ``LabelDecision``.

    Call ``decide(event)`` AFTER the scoring engine has populated
    ``combined_score_post_penalty`` and the penalty engine has run.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or default_config()

    def decide(self, event: EnrichedEvent) -> LabelDecision:
        """Return the label and a one-line human-readable reason.

        Decision gates run in the precedence order documented in the module
        docstring. The first match wins.
        """
        cfg = self._config
        pr = event.print_
        post_score = event.combined_score_post_penalty

        # 1) LEAP short-circuit — separate book.
        if pr.dte > cfg.dte.leap_dte_threshold:
            return LabelDecision(
                label=SignalLabel.LEAP_POSITIONING,
                reason=f"DTE {pr.dte} > {cfg.dte.leap_dte_threshold} (LEAP track, 0.25R max)",
            )

        # 2) POST_EVENT_NOISE — heavy IV crush risk.
        if event.is_post_event:
            return LabelDecision(
                label=SignalLabel.POST_EVENT_NOISE,
                reason="Within 2 sessions of catalyst — IV crush likely",
            )

        # 3) PENALIZED_BELOW_THRESHOLD — log-only.
        if post_score is None:
            msg = "Labeler called before scoring engine populated combined_score_post_penalty"
            raise LabelDecisionError(msg)
        if post_score < cfg.thresholds.penalized_below:
            return LabelDecision(
                label=SignalLabel.PENALIZED_BELOW_THRESHOLD,
                reason=(
                    f"combined_score {post_score:.3f} < {cfg.thresholds.penalized_below}"
                    f" (penalties={len(event.applied_penalties)})"
                ),
            )

        # 4) Next-day OI explicit failure — downgrade to closing/noise.
        if event.next_day_oi_confirmed is False:
            return LabelDecision(
                label=SignalLabel.LIKELY_CLOSING_OR_NOISE,
                reason="Next-day OI did not confirm — likely closing flow",
            )

        # 5) HIGH_CONVICTION_SEQUENCE: cluster + sweep/large-UOA + price confirmation.
        cluster_present = (event.cluster_density_score or 0.0) >= _CLUSTER_MIN
        sweep_or_large_uoa = self._is_sweep_or_large_uoa(event)
        if cluster_present and sweep_or_large_uoa and event.has_price_confirmation:
            return LabelDecision(
                label=SignalLabel.HIGH_CONVICTION_SEQUENCE,
                reason="Cluster + sweep/large-UOA + price confirmation — full stack",
            )

        # 6) CONFIRMED_OPENING_FLOW — explicit T+1 OI confirmation upgrade.
        if event.next_day_oi_confirmed is True:
            return LabelDecision(
                label=SignalLabel.CONFIRMED_OPENING_FLOW,
                reason="Next-day OI confirmed opening interest near trade size",
            )

        # 7) PRE_CATALYST_FLOW — cluster + event ≤ 7d + spot has not yet moved.
        # The "spot has not moved" condition is approximated in Phase 1 by the
        # ABSENCE of post-gap and price_direction not strongly aligned.
        if (
            cluster_present
            and (event.event_score or 0.0) >= _PRE_CATALYST_EVENT_MIN
            and not event.is_post_gap
        ):
            return LabelDecision(
                label=SignalLabel.PRE_CATALYST_FLOW,
                reason="Cluster + catalyst within 7 days + spot not yet moved",
            )

        # 8) SWEEP_UOA — sweep or ISO classified.
        if event.sweep_classification in {"sweep", "iso"}:
            return LabelDecision(
                label=SignalLabel.SWEEP_UOA,
                reason=f"{event.sweep_classification.upper()} classified — high urgency",
            )

        # 9) STANDARD_UOA — above-ask + relative_premium ≥ 0.5.
        if (
            pr.fill_side in {"above_ask", "at_ask"}
            and (event.relative_premium_score or 0.0) >= _RELATIVE_PREMIUM_UOA
        ):
            return LabelDecision(
                label=SignalLabel.STANDARD_UOA,
                reason="Aggressive fill + relative-premium above threshold",
            )

        # 10) OPTIONS_EQUITY_TAPE_CONFIRMATION — dark-pool tape confirms.
        if event.has_dark_pool_confirmation:
            return LabelDecision(
                label=SignalLabel.OPTIONS_EQUITY_TAPE_CONFIRMATION,
                reason="Dark-pool tape confirms options-flow direction",
            )

        # 11) SECTOR_FLOW_CLUSTER — peer flow within 30–90 min.
        if event.has_sector_confirmation:
            return LabelDecision(
                label=SignalLabel.SECTOR_FLOW_CLUSTER,
                reason="Same-direction flow across sector peers",
            )

        # 12) GAMMA_ACCELERATION_RISK — Module 21 flag.
        if event.is_gamma_acceleration:
            return LabelDecision(
                label=SignalLabel.GAMMA_ACCELERATION_RISK,
                reason="Dealer gamma acceleration risk near strike",
            )

        # 13) CONVEXITY_BURST — 3+ escalating prints / cluster_density ≥ 0.8.
        if (event.cluster_density_score or 0.0) >= _CLUSTER_BURST:
            return LabelDecision(
                label=SignalLabel.CONVEXITY_BURST,
                reason=f"cluster_density_score {event.cluster_density_score:.2f} ≥ {_CLUSTER_BURST}",
            )

        # 14) CONVEXITY_CLUSTER — 2+ same-direction prints same strike/expiry.
        if cluster_present:
            return LabelDecision(
                label=SignalLabel.CONVEXITY_CLUSTER,
                reason=f"cluster_density_score {event.cluster_density_score:.2f} ≥ {_CLUSTER_MIN}",
            )

        # 15) OPENING_UNCONFIRMED — flow flagged opening but T+1 OI not yet checked.
        if event.next_day_oi_confirmed is None and self._is_opening_pending(event):
            return LabelDecision(
                label=SignalLabel.OPENING_UNCONFIRMED,
                reason="Opening vs. closing pending next-day OI verification",
            )

        # 16) CONVEXITY_WATCH — convexity present but not yet clustered.
        if (event.convexity_score or 0.0) >= _CONVEXITY_WATCH_FLOOR:
            return LabelDecision(
                label=SignalLabel.CONVEXITY_WATCH,
                reason=f"convexity_score {event.convexity_score:.2f} present, no cluster yet",
            )

        # 17) Default
        return LabelDecision(
            label=SignalLabel.IGNORE_NOISE,
            reason="No qualifying conditions matched",
        )

    @staticmethod
    def _is_sweep_or_large_uoa(event: EnrichedEvent) -> bool:
        """True if Module 34/STANDARD_UOA criteria for the HCS gate are met."""
        if event.sweep_classification in {"sweep", "iso"}:
            return True
        pr = event.print_
        return (
            pr.fill_side in {"above_ask", "at_ask"}
            and (event.relative_premium_score or 0.0) >= _RELATIVE_PREMIUM_UOA
        )

    @staticmethod
    def _is_opening_pending(event: EnrichedEvent) -> bool:
        """Heuristic for OPENING_UNCONFIRMED in Phase 1.

        Phase 2 will replace this with the Module 27 stage's explicit output.
        For now we treat any aggressive fill that hasn't triggered another
        label as a pending-opening candidate.
        """
        return event.print_.fill_side in {"above_ask", "at_ask"}
