"""``CalibrationProfile`` — the single source of truth for every tunable number.

The Phase 1 ``config.py`` constants have all migrated into nested models here.
Profiles compose via ``inherits_from`` (deep-merge — child fields override
parent at leaf level only). Loaded from YAML by the resolver/loader.
"""

from __future__ import annotations

from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    """Base for every calibration sub-model. Strict (rejects unknown keys), frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class ScoringWeights(_StrictModel):
    """Combined-score formula weights (Part 3 of the v5 spec)."""

    uoa: float
    convexity: float
    event: float
    gamma: float
    price_confirmation: float
    sector_confirmation: float
    time_of_day: float


class EarlyScoringWeights(_StrictModel):
    """Early-convexity score weights (Part 3 of the v5 spec)."""

    convexity: float
    cluster_density: float
    event: float
    gamma: float
    time_of_day: float


# ---------------------------------------------------------------------------
# Penalties (Module 36) and contradiction
# ---------------------------------------------------------------------------

class PenaltyValues(_StrictModel):
    """Module 36 penalty deductions. All values are negative."""

    post_gap_move: float
    iv_rank_high: float
    wide_spread: float
    thin_oi: float
    flow_contradicts_price: float
    post_event: float
    isolated_print: float
    next_day_oi_failed: float


class ContradictionParams(_StrictModel):
    """Trigger conditions and magnitude for the contradiction penalty.

    Per Phase 1 decision, the contradiction is counted ONCE through the
    Module 36 list (``flow_contradicts_price``). This block exists so the
    magnitude is tunable and the rule itself is documented in profile form.
    """

    magnitude: float  # negative; alias for penalties.flow_contradicts_price
    enabled: bool = True


# ---------------------------------------------------------------------------
# Penalty trigger thresholds (Module 36 trigger conditions, separate from
# the deduction values themselves)
# ---------------------------------------------------------------------------

class PenaltyTriggers(_StrictModel):
    """Threshold values that decide whether each Module 36 penalty fires."""

    gap_threshold_pct: float  # >X% open → post-gap penalty
    iv_rank_threshold: int  # IV_rank >X → iv_rank_high penalty
    spread_pct_threshold: float  # spread >X% of mid → wide_spread penalty
    thin_oi_threshold: int  # OI <X → thin_oi penalty
    isolated_window_min: int  # No repeat within X min → isolated penalty
    next_day_oi_drop_pct: float  # OI drops >X% → next_day_oi_failed penalty
    post_event_sessions: int  # Within X sessions of catalyst → post_event


# ---------------------------------------------------------------------------
# DTE multipliers (Module 35)
# ---------------------------------------------------------------------------

class DTEMultipliers(_StrictModel):
    """DTE bucket multipliers, applied to convexity and gamma sub-scores only."""

    bucket_0_7: float
    bucket_8_14: float
    bucket_15_30: float
    bucket_31_60: float
    bucket_60_plus: float
    leap_threshold: int  # DTE > X → LEAP_POSITIONING short-circuit


# ---------------------------------------------------------------------------
# Time-of-day weights (Module 39)
# ---------------------------------------------------------------------------

class TimeWindow(_StrictModel):
    """One contiguous time-of-day window: half-open ``[start, end)``."""

    start: time
    end: time
    weight: float = Field(ge=0.0, le=1.0)
    label: str = Field(min_length=1, description="Stable name for the decision record.")

    @model_validator(mode="after")
    def _check_well_formed(self) -> TimeWindow:
        if self.start >= self.end:
            msg = f"window {self.label!r}: start {self.start} must be < end {self.end}"
            raise ValueError(msg)
        return self


class TimeOfDayWeights(_StrictModel):
    """Module 39 time-of-day weights — list of contiguous windows + outside default.

    The windows define the trading session for weighting purposes. Anything
    falling outside every window receives ``outside_session_weight`` (e.g.,
    pre-market, after-hours). The session timezone is parameterized via
    ``timezone`` (IANA name) so half-day sessions and non-US-equity profiles
    are first-class.

    **Spec-silent values**: the v5 spec defines five windows covering 09:30–16:00
    NY equity hours and is silent on prints outside that range. Both
    ``outside_session_weight`` and ``extended_hours_policy`` are tunables — see
    each field's description for the documented gap and Phase 1 inheritance.
    """

    timezone: str = Field(min_length=1, description="IANA timezone name, e.g. 'America/New_York'.")
    windows: tuple[TimeWindow, ...] = Field(min_length=1)
    outside_session_weight: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Spec-silent value. Default 0.30 inherited from Phase 1 (matches its "
            "lowest in-session weight) but the v5 spec defines no behavior for "
            "prints outside the configured windows. Treat as a tunable that the "
            "user should review per use case — see also ``extended_hours_policy``."
        ),
    )
    extended_hours_policy: Literal["weight", "flag", "reject"] = Field(
        default="flag",
        description=(
            "How Module 39 handles a print whose timestamp falls outside every "
            "configured window. 'weight' applies outside_session_weight silently "
            "(Phase 1 behavior). 'flag' applies outside_session_weight AND sets "
            "event.flags['extended_hours']=True for downstream filtering. "
            "'reject' drops the print entirely — no scoring, no labeling, no "
            "sizing — and records the rejection on event.rejection. Default "
            "'flag' preserves the numerical behavior while making the gap "
            "visible in every record."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            msg = f"unknown timezone {v!r}"
            raise ValueError(msg) from e
        return v

    @field_validator("windows")
    @classmethod
    def _validate_window_labels_unique(
        cls, v: tuple[TimeWindow, ...],
    ) -> tuple[TimeWindow, ...]:
        labels = [w.label for w in v]
        if len(set(labels)) != len(labels):
            msg = f"duplicate window labels: {labels}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _check_no_gaps_or_overlaps(self) -> TimeOfDayWeights:
        # Sort by start time and check each adjacent pair.
        sorted_windows = sorted(self.windows, key=lambda w: w.start)
        for i in range(len(sorted_windows) - 1):
            current, nxt = sorted_windows[i], sorted_windows[i + 1]
            if nxt.start < current.end:
                msg = (
                    f"windows overlap: {current.label!r} ends {current.end} "
                    f"but {nxt.label!r} starts {nxt.start}"
                )
                raise ValueError(msg)
            if nxt.start > current.end:
                msg = (
                    f"gap between windows: {current.label!r} ends {current.end} "
                    f"but {nxt.label!r} starts {nxt.start}"
                )
                raise ValueError(msg)
        return self

    def lookup(self, t: time) -> tuple[float, str]:
        """Return ``(weight, label)`` for time ``t``.

        If ``t`` falls outside every window, returns ``(outside_session_weight,
        "outside_session")``.
        """
        for w in self.windows:
            if w.start <= t < w.end:
                return w.weight, w.label
        return self.outside_session_weight, "outside_session"


# ---------------------------------------------------------------------------
# Cluster (Module 38)
# ---------------------------------------------------------------------------

class ClusterParams(_StrictModel):
    """Module 38 temporal-clustering parameters."""

    window_minutes: int = Field(description="Rolling window for cluster counting.")
    decay_minutes: int = Field(
        description="Minutes of inactivity after which cluster_density_score decays to zero.",
    )
    nearby_strike_count: int = Field(
        description="±N strikes (in chain grid units) counted as 'nearby strikes' for the 0.6 band.",
    )
    escalation_ratio: float = Field(
        description="Each successive print's premium must be at least 'ratio' times previous to count as escalating.",
    )
    density_isolated: float  # 1 print → 0.0
    density_two_same: float  # 2 prints same strike/expiry → 0.4
    density_three_same: float  # 3+ prints same strike/expiry → 0.8
    density_three_escalating: float  # 3+ with escalation → 1.0
    density_nearby_strikes: float  # across nearby strikes, same expiry → 0.6


# ---------------------------------------------------------------------------
# Sweep (Module 34)
# ---------------------------------------------------------------------------

class SweepParams(_StrictModel):
    """Module 34 sweep/block classification parameters."""

    iso_bonus: float  # +0.15 to uoa_score for ISO-tagged prints
    cross_venue_bonus: float  # +0.10 to uoa_score for multi-venue sweep
    cross_venue_above_ask_bonus: float  # +0.15 when above-ask AND cross-venue
    cross_venue_window_ms: int  # multi-exchange window for sweep detection


# ---------------------------------------------------------------------------
# Relative premium (Module 37)
# ---------------------------------------------------------------------------

class RelPremiumParams(_StrictModel):
    """Module 37 relative-premium scoring bands."""

    high_ratio: float  # ≥ X × median → score_high
    mid_ratio: float  # X × ≤ ratio < high_ratio → score_mid
    score_high: float  # 1.0 in v5
    score_mid: float  # 0.5 in v5
    score_low: float  # 0.0 in v5
    median_window_days: int  # rolling window for median trade size


# ---------------------------------------------------------------------------
# Label decision thresholds — replace ALL labeler hardcoded constants
# ---------------------------------------------------------------------------

class LabelThresholds(_StrictModel):
    """Every numeric threshold the labeler reads.

    After Phase 2 migration, ``labeling/labeler.py`` contains zero numerical
    literals — every comparison reads a value from this block.
    """

    penalized_below: float  # combined_score_post_penalty < X → PENALIZED
    cluster_min: float  # cluster_density_score ≥ X → cluster_present
    cluster_burst: float  # cluster_density_score ≥ X → CONVEXITY_BURST
    relative_premium_uoa: float  # relative_premium_score ≥ X → STANDARD_UOA
    convexity_watch_floor: float  # convexity_score ≥ X → CONVEXITY_WATCH
    pre_catalyst_event_min: float  # event_score ≥ X for PRE_CATALYST_FLOW


# ---------------------------------------------------------------------------
# Risk buckets (Part 5)
# ---------------------------------------------------------------------------

class RiskBuckets(_StrictModel):
    """Part 5 risk-bucket max-R values."""

    convexity_watch: float
    convexity_cluster: float  # also used for CONVEXITY_BURST per spec
    standard_uoa: float
    sweep_uoa: float
    pre_catalyst_flow: float
    high_conviction_sequence: float
    high_conviction_initial: float  # scale-in initial entry
    leap_positioning: float
    discard_or_log: float


# ---------------------------------------------------------------------------
# Fusion layer (Phase 2)
# ---------------------------------------------------------------------------

class FusionParams(_StrictModel):
    """SourceFusion windowing parameters."""

    window_ms: int = Field(
        description="Sliding-window duration for cross-source print reconciliation.",
    )
    timestamp_skew_tolerance_ms: int = Field(
        description="Max wall-clock skew between feeds before classification_disagreement.",
    )


# ---------------------------------------------------------------------------
# Sub-score missing-data behavior (Phase 2 — see decision B in the prompt)
# ---------------------------------------------------------------------------

class SubScoreMissingPolicy(_StrictModel):
    """Per-sub-score behavior when the upstream stage produced ``None``."""

    on_missing: Literal["default", "raise"]
    default: float = 0.0  # only consulted when on_missing == "default"

    @model_validator(mode="after")
    def _check_default_for_default_mode(self) -> SubScoreMissingPolicy:
        # 'raise' mode never reads the default; 'default' mode requires it
        # be inside the v5 [0,1] sub-score range. The check is loose because
        # custom profiles may extend the range.
        if self.on_missing == "default" and not (0.0 <= self.default <= 1.0):
            msg = "default value for on_missing='default' must be in [0,1]"
            raise ValueError(msg)
        return self


class SubScoreMissingBehavior(_StrictModel):
    """Per-field missing-data policy for every sub-score in the formula."""

    uoa_score: SubScoreMissingPolicy
    convexity_score: SubScoreMissingPolicy
    event_score: SubScoreMissingPolicy
    gamma_score: SubScoreMissingPolicy
    price_confirmation_score: SubScoreMissingPolicy
    sector_confirmation_score: SubScoreMissingPolicy
    time_of_day_weight: SubScoreMissingPolicy
    cluster_density_score: SubScoreMissingPolicy
    relative_premium_score: SubScoreMissingPolicy


# ---------------------------------------------------------------------------
# CalibrationProfile — top-level
# ---------------------------------------------------------------------------

class CalibrationProfile(_StrictModel):
    """Complete tunable surface of the detector. One YAML per profile."""

    profile_id: str
    description: str
    inherits_from: str | None = None

    scoring: ScoringWeights
    early_scoring: EarlyScoringWeights
    penalties: PenaltyValues
    penalty_triggers: PenaltyTriggers
    contradiction: ContradictionParams
    dte: DTEMultipliers
    time_of_day: TimeOfDayWeights
    cluster: ClusterParams
    sweep: SweepParams
    relative_premium: RelPremiumParams
    label_thresholds: LabelThresholds
    risk_buckets: RiskBuckets
    fusion: FusionParams
    sub_score_missing_behavior: SubScoreMissingBehavior

    @model_validator(mode="after")
    def _validate_invariants(self) -> CalibrationProfile:
        # Combined-score weights must sum to 1.0 ± 0.01
        s = self.scoring
        total = (
            s.uoa + s.convexity + s.event + s.gamma
            + s.price_confirmation + s.sector_confirmation + s.time_of_day
        )
        if abs(total - 1.0) > 0.01:
            msg = f"scoring weights must sum to 1.0 ± 0.01, got {total}"
            raise ValueError(msg)

        # Early-scoring weights must sum to 1.0 ± 0.01
        e = self.early_scoring
        e_total = e.convexity + e.cluster_density + e.event + e.gamma + e.time_of_day
        if abs(e_total - 1.0) > 0.01:
            msg = f"early_scoring weights must sum to 1.0 ± 0.01, got {e_total}"
            raise ValueError(msg)

        # Penalties must be ≤ 0
        for name in (
            "post_gap_move", "iv_rank_high", "wide_spread", "thin_oi",
            "flow_contradicts_price", "post_event", "isolated_print",
            "next_day_oi_failed",
        ):
            v = getattr(self.penalties, name)
            if v > 0:
                msg = f"penalties.{name} must be ≤ 0, got {v}"
                raise ValueError(msg)

        # DTE multipliers must be > 0
        for name in (
            "bucket_0_7", "bucket_8_14", "bucket_15_30",
            "bucket_31_60", "bucket_60_plus",
        ):
            v = getattr(self.dte, name)
            if v <= 0:
                msg = f"dte.{name} must be > 0, got {v}"
                raise ValueError(msg)

        # Cluster densities must be in [0,1]
        for name in (
            "density_isolated", "density_two_same", "density_three_same",
            "density_three_escalating", "density_nearby_strikes",
        ):
            v = getattr(self.cluster, name)
            if not (0.0 <= v <= 1.0):
                msg = f"cluster.{name} must be in [0,1], got {v}"
                raise ValueError(msg)

        return self
