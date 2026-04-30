"""Score containers — used by the scoring engine and stored in the backtest schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.domain.events import AppliedPenalty


class SubScores(BaseModel):
    """A snapshot of all v5 sub-scores at the moment of scoring.

    Mirrors the ``Sub-scores (v5)`` field group of the Module 29 backtest schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uoa_score: float = Field(ge=0.0, le=1.0)
    convexity_score: float = Field(ge=0.0, le=1.0)
    event_score: float = Field(ge=0.0, le=1.0)
    gamma_score: float = Field(ge=0.0, le=1.0)
    price_confirmation_score: float = Field(ge=0.0, le=1.0)
    sector_confirmation_score: float = Field(ge=0.0, le=1.0)
    time_of_day_weight: float = Field(ge=0.0, le=1.0)
    cluster_density_score: float = Field(ge=0.0, le=1.0)
    relative_premium_score: float = Field(ge=0.0, le=1.0)
    dte_multiplier_applied: float = Field(gt=0.0)


class CombinedScore(BaseModel):
    """Output of the scoring engine — both pre- and post-penalty values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    combined_score_pre_penalty: float
    combined_score_post_penalty: float
    applied_penalties: list[AppliedPenalty] = Field(default_factory=list)
    contradiction_penalty_applied: bool = False
