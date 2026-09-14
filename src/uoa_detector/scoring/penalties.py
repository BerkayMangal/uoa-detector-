"""Module 36 — Explicit Score Penalties (Phase 2 calibrated).

All eight v5 conditions, with thresholds and deduction values read from the
active ``CalibrationProfile``. No hardcoded numbers.
"""

from __future__ import annotations

from decimal import Decimal

from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent

_HUNDRED = Decimal("100")
_TWO = Decimal("2")


class PenaltyEngine:
    """Applies all eight Module 36 penalty conditions to an enriched event.

    The profile drives both the trigger thresholds (``profile.penalty_triggers``)
    and the deduction values (``profile.penalties``).
    """

    def __init__(self, profile: CalibrationProfile | None = None) -> None:
        self._profile = profile or load_default_profile()

    def apply(self, event: EnrichedEvent, *, iv_rank: float | None = None) -> EnrichedEvent:
        """Evaluate each penalty condition and append matches to ``event``.

        :param iv_rank: Optional 0–100 IV rank scalar from the IV-exhaustion
            module. ``None`` means "not measured" — the penalty is skipped.
        """
        cfg = self._profile
        p = cfg.penalties
        t = cfg.penalty_triggers
        pr = event.print_

        penalties: list[AppliedPenalty] = []

        # 1) Post-gap move (>X% open). Driven by upstream stage's is_post_gap.
        if event.is_post_gap:
            penalties.append(
                AppliedPenalty(
                    name="post_gap_move",
                    value=p.post_gap_move,
                    reason=f"Flow after >{t.gap_threshold_pct}% gap open",
                )
            )

        # 2) IV rank above threshold
        if iv_rank is not None and iv_rank > t.iv_rank_threshold:
            penalties.append(
                AppliedPenalty(
                    name="iv_rank_high",
                    value=p.iv_rank_high,
                    reason=f"IV_rank {iv_rank:.1f} exceeds threshold {t.iv_rank_threshold}",
                )
            )

        # 3) Wide bid/ask spread (> X% of mid). Decimal math; guard zero mid.
        mid = (pr.bid + pr.ask) / _TWO
        if mid > 0:
            spread_pct = ((pr.ask - pr.bid) / mid) * _HUNDRED
            if spread_pct > Decimal(str(t.spread_pct_threshold)):
                penalties.append(
                    AppliedPenalty(
                        name="wide_spread",
                        value=p.wide_spread,
                        reason=f"Spread {spread_pct:.1f}% > {t.spread_pct_threshold}% of mid",
                    )
                )

        # 4) Thin OI — skipped when OI is unknown (None): a pure
        # ThetaData replay has no open-interest, and "unknown" must
        # not be scored as "thin".
        if pr.open_interest is not None and pr.open_interest < t.thin_oi_threshold:
            penalties.append(
                AppliedPenalty(
                    name="thin_oi",
                    value=p.thin_oi,
                    reason=f"OI {pr.open_interest} below {t.thin_oi_threshold}",
                )
            )

        # 5) Flow direction contradicts price action
        contradicts = False
        if (pr.option_type == "call" and event.price_direction == "down") or (pr.option_type == "put" and event.price_direction == "up"):
            contradicts = True
        if contradicts:
            event.contradiction_penalty_applied = True
            penalties.append(
                AppliedPenalty(
                    name="flow_contradicts_price",
                    value=p.flow_contradicts_price,
                    reason=(
                        f"{pr.option_type} flow vs {event.price_direction}-trending price"
                    ),
                )
            )

        # 6) Post-event flow
        if event.is_post_event:
            penalties.append(
                AppliedPenalty(
                    name="post_event",
                    value=p.post_event,
                    reason=f"Flow within {t.post_event_sessions} sessions of catalyst",
                )
            )

        # 7) Isolated print (no repeat within isolated_window_min)
        if event.is_isolated_print:
            penalties.append(
                AppliedPenalty(
                    name="isolated_print",
                    value=p.isolated_print,
                    reason=f"No repeat within {t.isolated_window_min} min",
                )
            )

        # 8) Next-day OI failed
        if event.next_day_oi_confirmed is False:
            penalties.append(
                AppliedPenalty(
                    name="next_day_oi_failed",
                    value=p.next_day_oi_failed,
                    reason=f"Next-day OI dropped >{t.next_day_oi_drop_pct}% of trade size",
                )
            )

        event.applied_penalties.extend(penalties)
        return event
