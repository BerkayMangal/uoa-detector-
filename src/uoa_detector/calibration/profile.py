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
    # Phase 2.3.2a — separate bucket from DISCARD so backtest analysis can
    # distinguish "evaluated, no signal" from "not evaluated, out of scope".
    rejected: float


# ---------------------------------------------------------------------------
# Fusion layer (Phase 2)
# ---------------------------------------------------------------------------

class TierThresholds(_StrictModel):
    """Confidence-tier resolution thresholds for ``SourceAgreement.confidence_tier``.

    Tier resolution order (Phase 2.3.2 — user-specified):
      1. ``len(sources_seen) == 1`` → ``"single"`` (single-source path).
      2. All sources agree on judged fields → ``"unanimous"`` (requires
         ``len(sources_seen) >= unanimous_min_sources``).
      3. ``agreement_fraction >= majority_fraction`` → ``"majority"``.
      4. Otherwise → ``"conflicted"``.

    "Agreement" is judged on:
      - ``(strike, expiry, option_type)`` always (also bucket-key invariants).
      - ``premium_paid`` within ``premium_disagreement_tolerance_pct`` of the
        median across reporting sources.
      - ``sweep_classification`` if the source reports it (matches the modal
        value across reporting sources).

    Fields not all sources report (e.g., ISO flag, OI) do NOT count toward
    disagreement — only sources that report a field contribute to its agreement
    calculation.
    """

    unanimous_min_sources: int = Field(
        ge=2,
        description=(
            "Minimum number of sources for the result to be eligible for the "
            "'unanimous' tier. Default 2: with only 1 source, tier is 'single' "
            "regardless. Raise to 3+ for stricter unanimous requirement."
        ),
    )
    majority_fraction: float = Field(
        ge=0.5,
        le=1.0,
        description=(
            "Fraction of sources that must agree (on judged fields) for the "
            "'majority' tier. The agreement check is ``fraction >= "
            "majority_fraction`` so 0.5 means '>=50% (i.e., at least half) "
            "agree'. Setting > 0.5 enforces strict majority."
        ),
    )
    premium_disagreement_tolerance_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Premium values within this fraction of the median premium count "
            "as 'agreeing'. E.g., 0.05 = within 5%% of median. Spec-silent "
            "value — see Phase 2.3.2 prompt for rationale."
        ),
    )


class FusionParams(_StrictModel):
    """SourceFusion windowing parameters (event-time + watermark semantics)."""

    window_ms: int = Field(
        gt=0,
        description=(
            "Maximum span in event-time milliseconds within which prints are "
            "considered the same fusion bucket. Two prints with the same key "
            "fuse iff their event-time span (max-min across the bucket "
            "including both) is < window_ms."
        ),
    )
    timestamp_skew_tolerance_ms: int = Field(
        ge=0,
        description=(
            "Max wall-clock skew between feeds before classification_disagreement "
            "is flagged on the SourceAgreement (independent of bucket-fit decisions)."
        ),
    )
    stalled_source_timeout_ms: int = Field(
        gt=0,
        description=(
            "If a source produces no events for this many ms (walltime), its "
            "watermark is force-advanced to (walltime - allowed_lateness_ms) so "
            "one slow or dead source cannot block the global watermark, and "
            "buckets that have aged out can close. Spec-silent: the v5 doc "
            "doesn't define this — Phase 2.3.3 surfaces it as an explicit tunable."
        ),
    )
    allowed_lateness_ms: int = Field(
        ge=0,
        description=(
            "When force-advancing a stalled source's watermark, the watermark "
            "is set to (walltime - allowed_lateness_ms). This intentionally "
            "leaves room for slightly-late events to still be within global_wm. "
            "Tradeoff: larger values = more lateness tolerated, longer waits "
            "before stalled buckets close. Spec-silent."
        ),
    )
    tier_thresholds: TierThresholds = Field(
        description="Confidence-tier resolution thresholds — see TierThresholds.",
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
# Backtest configuration — Phase 3.2.3
# ---------------------------------------------------------------------------

HoldingStrategy = Literal["fixed_window", "dte_based", "take_profit_or_stop"]


class BacktestConfig(_StrictModel):
    """Backtest-only knobs: PnL pricing, holding strategy, walk-forward.

    Phase 3.2.3: introduces the section. SimplePnLProvider reads
    ``slippage_pct``, ``holding_strategy``, ``holding_window_days``,
    and ``exit_on_dte_lte``. The metric calculator reads
    ``walk_forward_windows`` and ``walk_forward_min_consistency_pct``.

    The defaults pinned here are the Track B + Formülasyon A values
    approved in Phase 3 prep step 3 + acceptance-doc 3.2.0b/3.2.3
    revisions. ``v5_default.yaml`` carries them; profiles that
    inherit and don't override get them unchanged. A pin test in
    ``tests/unit/test_calibration_profile.py`` (added in 3.2.3.1)
    asserts the defaults; override-via-yaml is supported but the
    defaults cannot drift without breaking the pin.
    """

    slippage_pct: float = Field(
        ge=0.0, le=0.5,
        description=(
            "Fraction of entry premium taken as slippage haircut on "
            "realized PnL. 0.02 = 2%."
        ),
    )
    holding_strategy: HoldingStrategy = Field(
        description=(
            "When to close a position: 'fixed_window' (after "
            "holding_window_days OR exit_on_dte_lte boundary), "
            "'dte_based' (close at a configured DTE threshold), "
            "'take_profit_or_stop' (Phase 3.4; raises NotImplementedError "
            "in 3.2.3 SimplePnLProvider)."
        ),
    )
    holding_window_days: int = Field(
        ge=1, le=60,
        description=(
            "fixed_window strategy: walltime days from entry until forced "
            "close. Track B's 3-5 day 'ignite or die' dynamic motivates "
            "the 5-day default."
        ),
    )
    exit_on_dte_lte: int = Field(
        ge=0, le=14,
        description=(
            "Hard safety floor: close any open position when its DTE "
            "drops to this threshold or below, regardless of strategy. "
            "Avoids modelling expiry-day gamma chaos / pin risk that "
            "the simple pricer cannot represent fairly."
        ),
    )
    dte_based_close_threshold: int = Field(
        ge=0, le=30,
        description=(
            "dte_based strategy only: close when DTE drops to this "
            "value. Distinct from exit_on_dte_lte (the safety floor); "
            "this is a tactical choice, not a hard floor. Ignored by "
            "fixed_window."
        ),
    )
    walk_forward_windows: int = Field(
        ge=2, le=64,
        description=(
            "Number of equal-trade-count windows the walk-forward "
            "consistency metric splits trades into. Default 8 over a "
            "2-year backtest = 3-month slices; doubles to 16 for 4 "
            "years, halves to 4 for 1 year."
        ),
    )
    walk_forward_min_consistency_pct: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Pass threshold for walk-forward consistency: fraction of "
            "windows whose expectancy E > 0. Default 0.75 means '6 of "
            "8 at default N=8' / '3 of 4 at N=4' / '12 of 16 at N=16'. "
            "N-independent by design."
        ),
    )


# ---------------------------------------------------------------------------
# Data source settings — Phase 3.3.2 (ThetaData), 3.3.3 (Unusual Whales)
# ---------------------------------------------------------------------------


class ThetaDataSettings(_StrictModel):
    """ThetaData adapter tunables.

    Phase 3.3.2: introduces the section. Defaults track Phase 3 prep
    + the ThetaData Pro plan limits documented at fetch time.

    All fields are non-credential (rate limits, retry policy). Real
    credentials live in env vars / .env, loaded by the ``Credentials``
    model in ``uoa_detector.config.credentials``. Two distinct
    surfaces, two distinct lifecycles.
    """

    rate_limit_requests_per_second: float = Field(
        default=10.0, gt=0.0, le=1000.0,
        description=(
            "Token-bucket rate limit for outbound HTTP/WS requests. "
            "10 req/s is conservative for the ThetaData Pro plan; "
            "raise after observing the actual quota in production."
        ),
    )
    historical_concurrency: int = Field(
        default=4, ge=1, le=32,
        description=(
            "Max concurrent historical-download tasks. Pro plan "
            "supports 4 in parallel; do not raise without re-reading "
            "ThetaData's terms."
        ),
    )
    live_reconnect_max_attempts: int = Field(
        default=5, ge=0, le=100,
        description=(
            "Max reconnect attempts on WebSocket disconnect before "
            "the live source raises and the operator must intervene. "
            "0 disables reconnect entirely (rare; useful for tests "
            "that want to assert a single failure)."
        ),
    )
    live_reconnect_initial_backoff_s: float = Field(
        default=1.0, gt=0.0, le=60.0,
        description=(
            "Initial backoff after first disconnect. Doubles on each "
            "subsequent attempt up to live_reconnect_max_backoff_s."
        ),
    )
    live_reconnect_max_backoff_s: float = Field(
        default=60.0, gt=0.0, le=3600.0,
        description="Ceiling for the exponential reconnect backoff.",
    )


class UnusualWhalesProviderCacheTTL(_StrictModel):
    """Per-provider-type cache TTL for the Unusual Whales adapter.

    Phase 3.3.3: introduces this section. UW responses are cached
    in-memory per provider instance keyed by request shape, with a
    monotonic-clock TTL. Different data types have different staleness
    tolerances:

      - ``catalyst_calendar``: events change rarely intraday → 1h.
      - ``dealer_gamma``: estimates updated through the day → 5min.
      - ``iv_history``: snapshots are minute-level → 10min.
      - ``sector_map``: ticker→sector is glacial → 24h.
      - ``dark_pool``: prints stream constantly; tight window → 60s.
      - ``open_interest``: end-of-day authoritative + intraday est → 10min.

    All values are in seconds. Set to 0 to disable caching for that
    provider (every request hits the API).
    """

    catalyst_calendar_seconds: int = Field(default=3600, ge=0, le=86_400)
    dealer_gamma_seconds: int = Field(default=300, ge=0, le=86_400)
    iv_history_seconds: int = Field(default=600, ge=0, le=86_400)
    sector_map_seconds: int = Field(default=86_400, ge=0, le=604_800)
    dark_pool_seconds: int = Field(default=60, ge=0, le=86_400)
    open_interest_seconds: int = Field(default=600, ge=0, le=86_400)


class UnusualWhalesSettings(_StrictModel):
    """Unusual Whales adapter tunables.

    Phase 3.3.3: introduces the section. Same shape as
    ``ThetaDataSettings`` for the rate-limit / reconnect surface, plus
    a nested ``cache_ttl`` block because UW exposes derived endpoints
    (calendar, gamma, IV, sector, DP, OI) that benefit from per-type
    caching.

    Credentials live in env vars / .env, loaded by ``Credentials``.
    """

    rate_limit_requests_per_second: float = Field(
        default=2.0, gt=0.0, le=1000.0,
        description=(
            "Token-bucket rate limit for outbound HTTP/WS requests. "
            "2 req/s is conservative for UW's API-Plus tier (which "
            "documents 120/min ≈ 2/s sustained); raise after observing "
            "the actual quota in production."
        ),
    )
    historical_concurrency: int = Field(
        default=2, ge=1, le=32,
        description=(
            "Max concurrent historical-pull tasks (e.g. backfill "
            "loops). Lower than ThetaData's 4 because UW endpoints "
            "are aggregated/derived and respond more slowly."
        ),
    )
    live_reconnect_max_attempts: int = Field(
        default=5, ge=0, le=100,
        description=(
            "Max reconnect attempts on WebSocket disconnect. Same "
            "semantics as ThetaData's field."
        ),
    )
    live_reconnect_initial_backoff_s: float = Field(
        default=1.0, gt=0.0, le=60.0,
        description="Initial backoff after first disconnect.",
    )
    live_reconnect_max_backoff_s: float = Field(
        default=60.0, gt=0.0, le=3600.0,
        description="Ceiling for the exponential reconnect backoff.",
    )
    cache_ttl: UnusualWhalesProviderCacheTTL = Field(
        default_factory=UnusualWhalesProviderCacheTTL,
    )


class DataSourcesConfig(_StrictModel):
    """Per-source adapter tunables.

    Phase 3.3.2 shipped ``thetadata``. Phase 3.3.3 adds
    ``unusual_whales``. Profiles that don't need a particular
    source can omit the override and inherit the defaults.
    """

    thetadata: ThetaDataSettings = Field(default_factory=ThetaDataSettings)
    unusual_whales: UnusualWhalesSettings = Field(
        default_factory=UnusualWhalesSettings,
    )


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
    backtest: BacktestConfig
    data_sources: DataSourcesConfig = Field(default_factory=DataSourcesConfig)

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

    def content_hash(self) -> str:
        """SHA-256 of the profile's canonical-JSON serialization.

        Used in the ``SignalDecisionRecord`` so the operator can reconcile
        a stored decision against the exact profile that produced it.
        Profiles with the same field values produce the same hash regardless
        of in-memory object identity. Sort_keys=True ensures field order
        doesn't affect the hash.
        """
        import hashlib

        canonical = self.model_dump_json(by_alias=False)
        # model_dump_json output is non-deterministic across Pydantic versions
        # only in formatting; sort by re-loading and re-dumping with sorted keys.
        import json

        parsed = json.loads(canonical)
        canonical_sorted = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_sorted.encode("utf-8")).hexdigest()
