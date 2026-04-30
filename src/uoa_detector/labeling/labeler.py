"""Full v5 labeler — Phase 2 calibrated.

Every numeric comparison reads from ``profile.label_thresholds.*`` or
``profile.dte.leap_threshold``. Zero numerical literals in this file.

Precedence (top wins):
1.  ``LEAP_POSITIONING``                 — DTE > profile.dte.leap_threshold
2.  ``POST_EVENT_NOISE``                 — within 2 sessions of catalyst
3.  ``PENALIZED_BELOW_THRESHOLD``        — combined < profile.label_thresholds.penalized_below
4.  ``LIKELY_CLOSING_OR_NOISE``          — next_day_oi_confirmed is False
5.  ``HIGH_CONVICTION_SEQUENCE``         — cluster + sweep/large-UOA + price confirm
6.  ``CONFIRMED_OPENING_FLOW``           — next_day_oi_confirmed is True (upgrade)
7.  ``PRE_CATALYST_FLOW``                — cluster + event + spot not moved
8.  ``SWEEP_UOA``                        — sweep_classification in {sweep, iso}
9.  ``STANDARD_UOA``                     — above-ask + relative_premium ≥ threshold
10. ``OPTIONS_EQUITY_TAPE_CONFIRMATION`` — has_dark_pool_confirmation
11. ``SECTOR_FLOW_CLUSTER``              — has_sector_confirmation
12. ``GAMMA_ACCELERATION_RISK``          — is_gamma_acceleration flag
13. ``CONVEXITY_BURST``                  — cluster_density_score ≥ cluster_burst
14. ``CONVEXITY_CLUSTER``                — cluster_density_score ≥ cluster_min
                                           (downgraded to CONVEXITY_WATCH if cluster_decayed)
15. ``OPENING_UNCONFIRMED``              — opening/closing pending T+1
16. ``CONVEXITY_WATCH``                  — convexity_score ≥ convexity_watch_floor
17. ``IGNORE_NOISE``                     — fallback
"""

from __future__ import annotations

from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.errors import LabelDecisionError


class Labeler:
    """Profile-driven labeler. All thresholds come from ``profile.label_thresholds``."""

    def __init__(self, profile: CalibrationProfile | None = None) -> None:
        self._profile = profile or load_default_profile()

    def decide(self, event: EnrichedEvent) -> LabelDecision:
        """Return the label and a one-line human-readable reason."""
        cfg = self._profile
        t = cfg.label_thresholds
        pr = event.print_
        post_score = event.combined_score_post_penalty

        # 1) LEAP short-circuit — separate book.
        if pr.dte > cfg.dte.leap_threshold:
            return LabelDecision(
                label=SignalLabel.LEAP_POSITIONING,
                reason=f"DTE {pr.dte} > {cfg.dte.leap_threshold} (LEAP track)",
            )

        # 2) POST_EVENT_NOISE — heavy IV crush risk.
        if event.is_post_event:
            return LabelDecision(
                label=SignalLabel.POST_EVENT_NOISE,
                reason="Within configured sessions of catalyst — IV crush likely",
            )

        # 3) PENALIZED_BELOW_THRESHOLD — log-only.
        if post_score is None:
            msg = "Labeler called before scoring engine populated combined_score_post_penalty"
            raise LabelDecisionError(msg)
        if post_score < t.penalized_below:
            return LabelDecision(
                label=SignalLabel.PENALIZED_BELOW_THRESHOLD,
                reason=(
                    f"combined_score {post_score:.3f} < {t.penalized_below}"
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
        cluster_present = (event.cluster_density_score or 0.0) >= t.cluster_min
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

        # 7) PRE_CATALYST_FLOW — cluster + imminent event + spot not yet moved.
        if (
            cluster_present
            and (event.event_score or 0.0) >= t.pre_catalyst_event_min
            and not event.is_post_gap
        ):
            return LabelDecision(
                label=SignalLabel.PRE_CATALYST_FLOW,
                reason="Cluster + catalyst within configured window + spot not yet moved",
            )

        # 8) SWEEP_UOA — sweep or ISO classified.
        if event.sweep_classification in {"sweep", "iso"}:
            return LabelDecision(
                label=SignalLabel.SWEEP_UOA,
                reason=f"{event.sweep_classification.upper()} classified — high urgency",
            )

        # 9) STANDARD_UOA — above-ask + relative_premium ≥ threshold.
        if (
            pr.fill_side in {"above_ask", "at_ask"}
            and (event.relative_premium_score or 0.0) >= t.relative_premium_uoa
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

        # 11) SECTOR_FLOW_CLUSTER — peer flow within configured window.
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

        # 13) CONVEXITY_BURST — escalating cluster.
        if (event.cluster_density_score or 0.0) >= t.cluster_burst:
            return LabelDecision(
                label=SignalLabel.CONVEXITY_BURST,
                reason=(
                    f"cluster_density_score {event.cluster_density_score:.2f} "
                    f"≥ {t.cluster_burst}"
                ),
            )

        # 14) CONVEXITY_CLUSTER (or CONVEXITY_WATCH if decayed)
        if cluster_present:
            if event.cluster_decayed:
                return LabelDecision(
                    label=SignalLabel.CONVEXITY_WATCH,
                    reason=(
                        "Cluster decayed (no qualifying prints within configured timeout) "
                        "— downgraded to CONVEXITY_WATCH"
                    ),
                )
            return LabelDecision(
                label=SignalLabel.CONVEXITY_CLUSTER,
                reason=(
                    f"cluster_density_score {event.cluster_density_score:.2f} "
                    f"≥ {t.cluster_min}"
                ),
            )

        # 15) OPENING_UNCONFIRMED
        if event.next_day_oi_confirmed is None and self._is_opening_pending(event):
            return LabelDecision(
                label=SignalLabel.OPENING_UNCONFIRMED,
                reason="Opening vs. closing pending next-day OI verification",
            )

        # 16) CONVEXITY_WATCH
        if (event.convexity_score or 0.0) >= t.convexity_watch_floor:
            return LabelDecision(
                label=SignalLabel.CONVEXITY_WATCH,
                reason=(
                    f"convexity_score {event.convexity_score:.2f} present, no cluster yet"
                ),
            )

        # 17) Default
        return LabelDecision(
            label=SignalLabel.IGNORE_NOISE,
            reason="No qualifying conditions matched",
        )

    def _is_sweep_or_large_uoa(self, event: EnrichedEvent) -> bool:
        """True if Module 34 / STANDARD_UOA criteria for the HCS gate are met."""
        if event.sweep_classification in {"sweep", "iso"}:
            return True
        pr = event.print_
        return (
            pr.fill_side in {"above_ask", "at_ask"}
            and (event.relative_premium_score or 0.0) >= self._profile.label_thresholds.relative_premium_uoa
        )

    @staticmethod
    def _is_opening_pending(event: EnrichedEvent) -> bool:
        """Heuristic for OPENING_UNCONFIRMED until Module 27 lands in Phase 3."""
        return event.print_.fill_side in {"above_ask", "at_ask"}
