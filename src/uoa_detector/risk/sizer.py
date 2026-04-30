"""Risk sizer — maps each ``SignalLabel`` to a ``PositionSize`` per Part 5.

Risk buckets (verbatim from the v5 spec):

==================================  ========================================
Label                               Max risk
==================================  ========================================
CONVEXITY_WATCH                      0.25R
CONVEXITY_CLUSTER / CONVEXITY_BURST  0.50R
STANDARD_UOA                         0.75R
SWEEP_UOA                            0.85R
PRE_CATALYST_FLOW                    0.85R
HIGH_CONVICTION_SEQUENCE             1.00R (scale-in: 0.50R initial,
                                            0.50R after price confirmation)
LEAP_POSITIONING                     0.25R
All discard / log labels             0.0R
==================================  ========================================

Discard / log labels are: ``IGNORE_NOISE``, ``LIKELY_CLOSING_OR_NOISE``,
``POST_EVENT_NOISE``, ``PENALIZED_BELOW_THRESHOLD``,
``OPENING_UNCONFIRMED`` (flag only — verify next day),
``SECTOR_FLOW_CLUSTER`` (thesis flag, no direct size in Phase 1),
``OPTIONS_EQUITY_TAPE_CONFIRMATION`` (additive, no direct size),
``CONFIRMED_OPENING_FLOW`` (upgrade flag, sizing follows the underlying
trade — Phase 1 returns 0.0R for this until upgrade-stacking lands),
``GAMMA_ACCELERATION_RISK`` (risk warning, no direct size).
"""

from __future__ import annotations

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket


class RiskSizer:
    """Map a ``SignalLabel`` to a ``PositionSize``."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or default_config()

    def size_for(self, label: SignalLabel) -> PositionSize:
        """Return the ``PositionSize`` that applies to ``label``."""
        r = self._config.risk

        if label == SignalLabel.HIGH_CONVICTION_SEQUENCE:
            # Scale-in: 0.50R initial, +0.50R after price confirmation.
            return PositionSize(
                bucket=RiskBucket.HIGH_CONVICTION_SEQUENCE,
                max_r=r.high_conviction_sequence,
                scale_in=True,
                initial_r=r.high_conviction_initial,
            )

        if label == SignalLabel.SWEEP_UOA:
            return PositionSize(bucket=RiskBucket.SWEEP_UOA, max_r=r.sweep_uoa)

        if label == SignalLabel.PRE_CATALYST_FLOW:
            return PositionSize(bucket=RiskBucket.PRE_CATALYST_FLOW, max_r=r.pre_catalyst_flow)

        if label == SignalLabel.STANDARD_UOA:
            return PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=r.standard_uoa)

        if label in {SignalLabel.CONVEXITY_CLUSTER, SignalLabel.CONVEXITY_BURST}:
            return PositionSize(
                bucket=RiskBucket.CONVEXITY_CLUSTER,
                max_r=r.convexity_cluster,  # same as convexity_burst
            )

        if label == SignalLabel.CONVEXITY_WATCH:
            return PositionSize(bucket=RiskBucket.CONVEXITY_WATCH, max_r=r.convexity_watch)

        if label == SignalLabel.LEAP_POSITIONING:
            return PositionSize(bucket=RiskBucket.LEAP_POSITIONING, max_r=r.leap_positioning)

        # All other labels are flags / log-only / additive — no direct sizing in Phase 1.
        # See module docstring for the complete list.
        return PositionSize(bucket=RiskBucket.DISCARD, max_r=r.discard_or_log)
