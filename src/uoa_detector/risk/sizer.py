"""Risk sizer (Phase 2 calibrated) — maps each ``SignalLabel`` to a ``PositionSize``.

Reads every max-R from ``profile.risk_buckets``. Zero numerical literals here.
"""

from __future__ import annotations

from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket


class RiskSizer:
    """Map a ``SignalLabel`` to a ``PositionSize`` per the active profile."""

    def __init__(self, profile: CalibrationProfile | None = None) -> None:
        self._profile = profile or load_default_profile()

    def size_for(self, label: SignalLabel) -> PositionSize:
        """Return the ``PositionSize`` that applies to ``label``."""
        r = self._profile.risk_buckets

        if label == SignalLabel.HIGH_CONVICTION_SEQUENCE:
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
                max_r=r.convexity_cluster,
            )

        if label == SignalLabel.CONVEXITY_WATCH:
            return PositionSize(bucket=RiskBucket.CONVEXITY_WATCH, max_r=r.convexity_watch)

        if label == SignalLabel.LEAP_POSITIONING:
            return PositionSize(bucket=RiskBucket.LEAP_POSITIONING, max_r=r.leap_positioning)

        # All other labels are flags / log-only / additive — no direct sizing.
        return PositionSize(bucket=RiskBucket.DISCARD, max_r=r.discard_or_log)
