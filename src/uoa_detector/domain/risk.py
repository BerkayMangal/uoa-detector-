"""Risk-management types: ``RiskBucket`` (label → max-R) and ``PositionSize`` (sized output)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskBucket(StrEnum):
    """Symbolic risk-bucket names — kept for telemetry and configurability."""

    DISCARD = "DISCARD"
    CONVEXITY_WATCH = "CONVEXITY_WATCH"
    CONVEXITY_CLUSTER = "CONVEXITY_CLUSTER"  # also covers CONVEXITY_BURST
    STANDARD_UOA = "STANDARD_UOA"
    SWEEP_UOA = "SWEEP_UOA"
    PRE_CATALYST_FLOW = "PRE_CATALYST_FLOW"
    HIGH_CONVICTION_SEQUENCE = "HIGH_CONVICTION_SEQUENCE"
    LEAP_POSITIONING = "LEAP_POSITIONING"

    # Phase 2.3.2a — distinct from DISCARD: REJECTED means the system did NOT
    # evaluate the print (out of scope by policy), whereas DISCARD covers
    # IGNORE_NOISE et al. (evaluated, no actionable signal).
    REJECTED = "REJECTED"


class PositionSize(BaseModel):
    """Sized position output. ``max_r`` is the full target; ``initial_r`` is the
    first tranche when ``scale_in`` is ``True`` (HIGH_CONVICTION_SEQUENCE only).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bucket: RiskBucket
    max_r: float = Field(ge=0.0, le=1.0)
    scale_in: bool = False
    initial_r: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_scale_in(self) -> PositionSize:
        if self.scale_in and self.initial_r is None:
            msg = "scale_in=True requires initial_r to be set"
            raise ValueError(msg)
        if self.scale_in and self.initial_r is not None and self.initial_r > self.max_r:
            msg = "initial_r cannot exceed max_r"
            raise ValueError(msg)
        if not self.scale_in and self.initial_r is not None:
            msg = "initial_r must be None when scale_in is False"
            raise ValueError(msg)
        return self
