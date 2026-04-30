"""Module 36 — Explicit Score Penalties (FULL implementation).

Eight penalty conditions, exact values from the v5 spec:

============================================================  ========
Condition                                                     Penalty
============================================================  ========
Flow appears after large gap move (>3% open)                   −0.20
IV_rank already >80 at signal time                             −0.15
Bid/ask spread >15% of mid                                     −0.15
Open interest <100 at strike (thin chain)                      −0.20
Flow direction contradicts price action                        −0.15
Post-earnings flow (within 2 sessions of print)                −0.25
Single isolated print — no repeat within 30 min                −0.10
Next-day OI does not confirm (OI drops >30% of trade size)     −0.20
============================================================  ========

The engine populates ``event.applied_penalties`` and sets
``event.contradiction_penalty_applied = True`` when the contradiction
condition fires. Each penalty is recorded as exactly one
``AppliedPenalty`` entry — no double-counting.
"""

from __future__ import annotations

from decimal import Decimal

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent

_HUNDRED = Decimal("100")


class PenaltyEngine:
    """Applies all eight Module 36 penalty conditions to an enriched event.

    Inputs come from the event itself (booleans set by upstream stages plus
    the underlying print's static fields). External signals like IV rank, gap
    presence, post-event-ness, and next-day OI confirmation are surfaced via
    the boolean flags on ``EnrichedEvent``.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or default_config()

    def apply(self, event: EnrichedEvent, *, iv_rank: float | None = None) -> EnrichedEvent:
        """Evaluate each penalty condition and append matches to ``event``.

        :param iv_rank: Optional 0–100 IV rank scalar from the IV exhaustion
            module. ``None`` means "not measured" — the penalty is skipped.
        """
        cfg = self._config
        p = cfg.penalties
        t = cfg.thresholds
        pr = event.print_

        penalties: list[AppliedPenalty] = []

        # 1) Post-gap move (>3% open). Driven by the M23 stage flagging is_post_gap.
        if event.is_post_gap:
            penalties.append(
                AppliedPenalty(
                    name="post_gap_move",
                    value=p.post_gap_move,
                    reason=f"Flow after >{t.gap_threshold_pct}% gap open",
                )
            )

        # 2) IV rank > 80
        if iv_rank is not None and iv_rank > t.iv_rank_threshold:
            penalties.append(
                AppliedPenalty(
                    name="iv_rank_high",
                    value=p.iv_rank_high,
                    reason=f"IV_rank {iv_rank:.1f} exceeds threshold {t.iv_rank_threshold}",
                )
            )

        # 3) Wide bid/ask spread (> 15% of mid). Decimal math; guard zero mid.
        mid = (pr.bid + pr.ask) / Decimal(2)
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

        # 4) Thin OI (<100 at strike)
        if pr.open_interest < t.thin_oi_threshold:
            penalties.append(
                AppliedPenalty(
                    name="thin_oi",
                    value=p.thin_oi,
                    reason=f"OI {pr.open_interest} below {t.thin_oi_threshold}",
                )
            )

        # 5) Flow direction contradicts price action.
        # Calls with downtrend, or puts with uptrend.
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

        # 6) Post-event flow (within 2 sessions)
        if event.is_post_event:
            penalties.append(
                AppliedPenalty(
                    name="post_event",
                    value=p.post_event,
                    reason=f"Flow within {t.post_event_sessions} sessions of catalyst",
                )
            )

        # 7) Isolated print (no repeat within 30 min)
        if event.is_isolated_print:
            penalties.append(
                AppliedPenalty(
                    name="isolated_print",
                    value=p.isolated_print,
                    reason=f"No repeat within {t.isolated_window_min} min",
                )
            )

        # 8) Next-day OI does not confirm (drops >30% of trade size).
        # Only fires on explicit failure; None means "not yet evaluated".
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
