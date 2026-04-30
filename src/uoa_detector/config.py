"""Typed configuration. All v5 numeric constants live here, sourced from the spec.

Per the spec, numbers must match v5 exactly. If you find yourself editing a value
in this file because a test fails, you almost certainly have a bug elsewhere — the
v5 numbers are authoritative.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CombinedScoreWeights(BaseModel):
    """Weights for the v5 combined-score formula (Part 3 of the spec)."""

    uoa: float = 0.30
    convexity: float = 0.25
    event: float = 0.15
    gamma: float = 0.10
    price_confirmation: float = 0.10
    sector_confirmation: float = 0.05
    time_of_day: float = 0.05


class EarlyConvexityWeights(BaseModel):
    """Weights for the early-stage convexity score (Part 3 of the spec)."""

    convexity: float = 0.40
    cluster_density: float = 0.20
    event: float = 0.15
    gamma: float = 0.15
    time_of_day: float = 0.10


class PenaltyValues(BaseModel):
    """Module 36 penalty deductions. All values are negative (deductions)."""

    post_gap_move: float = -0.20  # Flow appears after >3% gap open
    iv_rank_high: float = -0.15  # IV_rank already >80 at signal time
    wide_spread: float = -0.15  # Bid/ask spread >15% of mid
    thin_oi: float = -0.20  # Open interest <100 at strike
    flow_contradicts_price: float = -0.15  # Flow direction contradicts price action
    post_event: float = -0.25  # Post-earnings flow within 2 sessions
    isolated_print: float = -0.10  # Single isolated print, no repeat within 30 min
    next_day_oi_failed: float = -0.20  # Next-day OI does not confirm (drops >30%)


class DTEMultipliers(BaseModel):
    """Module 35 DTE decay multipliers. Applied to convexity_score and gamma_score only."""

    bucket_0_7: float = 1.20  # 0–7 DTE
    bucket_8_14: float = 1.10  # 8–14 DTE
    bucket_15_30: float = 1.00  # 15–30 DTE — baseline
    bucket_31_60: float = 0.85  # 31–60 DTE
    bucket_60_plus: float = 0.70  # >60 DTE (and ≤90; >90 routes to LEAP)
    leap_dte_threshold: int = 90  # DTE > 90 → LEAP_POSITIONING


class TimeOfDayWeights(BaseModel):
    """Module 39 time-of-day weights. Times are EST/EDT (America/New_York)."""

    open_auction: float = 0.30  # 09:30–10:00
    early_session: float = 0.80  # 10:00–11:00
    prime: float = 1.00  # 11:00–14:00
    afternoon: float = 0.70  # 14:00–15:30
    moc_loc: float = 0.30  # 15:30–16:00


class RiskBuckets(BaseModel):
    """Part 5 risk buckets. Max-R per label."""

    convexity_watch: float = 0.25
    convexity_cluster: float = 0.50
    convexity_burst: float = 0.50
    standard_uoa: float = 0.75
    sweep_uoa: float = 0.85
    pre_catalyst_flow: float = 0.85
    high_conviction_sequence: float = 1.00
    high_conviction_initial: float = 0.50  # Scale-in: initial entry
    leap_positioning: float = 0.25
    discard_or_log: float = 0.0


class Thresholds(BaseModel):
    """Misc thresholds drawn from the spec."""

    penalized_below: float = 0.30  # combined_score post-penalty < 0.30 → PENALIZED
    contradiction_penalty: float = -0.15  # Module 36 + Part 3 contradiction rule
    gap_threshold_pct: float = 3.0  # >3% gap → post-gap penalty
    iv_rank_threshold: int = 80  # IV_rank >80
    spread_pct_threshold: float = 15.0  # spread >15% of mid
    thin_oi_threshold: int = 100  # OI <100
    isolated_window_min: int = 30  # No repeat within 30 min → isolated
    next_day_oi_drop_pct: float = 30.0  # OI drops >30% → next-day failure
    cluster_window_min: int = 60  # Module 38 60-min window
    cluster_decay_min: int = 90  # Module 38 90-min decay
    post_event_sessions: int = 2  # Within 2 sessions of catalyst
    relative_premium_high: float = 3.0  # ≥3× median → score 1.0
    relative_premium_mid: float = 1.5  # 1.5–3× median → score 0.5

    # Sub-score range — see ScoreOutOfRangeError
    score_min: float = 0.0
    score_max: float = 1.0

    # Premium decimals
    premium_quantize: Decimal = Field(default=Decimal("0.01"))


class AppConfig(BaseSettings):
    """Top-level application config."""

    model_config = SettingsConfigDict(
        env_prefix="UOA_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    weights: CombinedScoreWeights = Field(default_factory=CombinedScoreWeights)
    early_weights: EarlyConvexityWeights = Field(default_factory=EarlyConvexityWeights)
    penalties: PenaltyValues = Field(default_factory=PenaltyValues)
    dte: DTEMultipliers = Field(default_factory=DTEMultipliers)
    tod: TimeOfDayWeights = Field(default_factory=TimeOfDayWeights)
    risk: RiskBuckets = Field(default_factory=RiskBuckets)
    thresholds: Thresholds = Field(default_factory=Thresholds)


def default_config() -> AppConfig:
    """Return the default v5 configuration."""
    return AppConfig()
