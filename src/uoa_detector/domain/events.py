"""Core event models: ``OptionsPrint`` (canonical) and ``EnrichedEvent`` (pipeline state).

Phase 2 changes:
  - ``OptionsPrint.source_agreement`` is now a required field (single-source
    paths use ``single_source_agreement(source_id)``).
  - ``EnrichedEvent.is_escalating_cluster`` → ``escalating_cluster`` (renamed
    per Phase 2 naming standard — no ``is_`` prefix).
  - ``EnrichedEvent.score_adjustments: list[ScoreAdjustment]`` — additive
    sub-score adjustments produced by stages like Module 34, applied inside
    the scoring engine rather than mutating sub-scores directly.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uoa_detector.domain.agreement import ScoreAdjustment, SourceAgreement
from uoa_detector.domain.rejection import RejectedEvent

FillSide = Literal["above_ask", "at_ask", "midpoint", "at_bid", "below_bid", "unknown"]
OptionType = Literal["call", "put"]
SweepClassification = Literal["block", "sweep", "iso"]


class OptionsPrint(BaseModel):
    """The canonical, fusion-reconciled options print fed into the pipeline.

    Constructed by ``SourceFusion`` from one or more ``RawPrint`` observations.
    Every print carries a ``SourceAgreement`` describing how feeds agreed —
    single-source mode produces ``confidence_tier="single"`` with zero skew.
    Decimal is used for prices/premiums; floats are reserved for scores and IV.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    timestamp: datetime  # tz-aware UTC; see validator
    ticker: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    dte: int = Field(ge=0, description="Calendar days from timestamp.date() to expiry.")
    spot_price: Decimal
    premium_paid: Decimal = Field(description="Notional dollar size of the print.")
    option_price: Decimal
    implied_volatility: float = Field(ge=0.0)
    bid: Decimal
    ask: Decimal
    fill_side: FillSide = "unknown"
    exchange: str
    is_iso: bool = False
    open_interest: int = Field(ge=0)

    # Phase 2: required first-class metadata
    source_agreement: SourceAgreement

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "OptionsPrint.timestamp must be tz-aware (UTC)"
            raise ValueError(msg)
        return v

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, v: str) -> str:
        if not v.strip():
            msg = "ticker must not be empty"
            raise ValueError(msg)
        return v.strip().upper()


class AppliedPenalty(BaseModel):
    """A single penalty applied by the penalty engine (Module 36)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float = Field(le=0.0, description="Negative deduction, e.g. -0.20")
    reason: str


class EnrichedEvent(BaseModel):
    """Mutable pipeline state. Stages populate sub-score fields in-place.

    Each sub-score is ``None`` until the responsible stage has run. Phase 2's
    scoring engine consults ``profile.sub_score_missing_behavior`` to decide
    whether ``None`` raises (bug indicator) or substitutes a configured
    default (data-gap policy). See ``scoring/combined.py``.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=False)

    print_: OptionsPrint = Field(alias="print")

    # Sub-scores (populated by enrichment stages, all in [0,1] except multipliers)
    uoa_score: float | None = None
    convexity_score: float | None = None
    event_score: float | None = None
    gamma_score: float | None = None
    price_confirmation_score: float | None = None
    sector_confirmation_score: float | None = None
    time_of_day_weight: float | None = None
    cluster_density_score: float | None = None
    relative_premium_score: float | None = None

    # Phase 2: additive adjustments produced by stages (e.g., Module 34 sweep bonus).
    # Applied inside the scoring engine; the original sub-score is preserved.
    score_adjustments: list[ScoreAdjustment] = Field(default_factory=list)

    # Phase 2: which time-of-day window the event fell into (e.g., "prime_session"
    # or "outside_session"). Populated by Module 39; surfaced in the decision
    # record for downstream filtering.
    time_window_label: str | None = None

    # Multiplier (Module 35) — applied inside the scoring engine to convexity & gamma
    dte_multiplier_applied: float | None = None

    # Classification flags
    sweep_classification: SweepClassification | None = None

    # Boolean enrichments used by penalty engine and labeler
    is_post_event: bool = False
    is_post_gap: bool = False
    is_isolated_print: bool = False
    next_day_oi_confirmed: bool | None = None  # None = not yet validated

    # Direction signal supplied by Module 23 (price confirmation stage).
    # Values: "up" (HH/HL), "down" (LH/LL), "neutral".
    price_direction: Literal["up", "down", "neutral"] | None = None

    # Populated by penalty engine + scoring engine
    applied_penalties: list[AppliedPenalty] = Field(default_factory=list)
    contradiction_penalty_applied: bool = False
    combined_score_pre_penalty: float | None = None
    combined_score_post_penalty: float | None = None

    # Phase 2 missing-data tracking — populated by the scoring engine when
    # a sub-score was None and policy was on_missing="default".
    missing_sub_scores: list[str] = Field(default_factory=list)

    # Auxiliary signals used by stages and labeler. Kept narrow and typed.
    has_price_confirmation: bool = False  # Module 23 → labeler / HCS test
    has_sector_confirmation: bool = False  # Module 25 → SECTOR_FLOW_CLUSTER
    has_dark_pool_confirmation: bool = False  # Module 26 → OPTIONS_EQUITY_TAPE_CONFIRMATION
    is_gamma_acceleration: bool = False  # Module 21 GAMMA_ACCELERATION_RISK flag
    escalating_cluster: bool = False  # Module 38 escalating-size flag (Phase 2 rename)

    # Phase 2 cluster decay — set by ClusterDecayWatcher when it emits a
    # synthetic decay event so the labeler can downgrade CONVEXITY_CLUSTER
    # back to CONVEXITY_WATCH after the configured timeout.
    cluster_decayed: bool = False

    # Phase 2.3.1: structured rejection marker. When set, the pipeline
    # orchestrator skips all remaining enrichment, scoring, penalties,
    # labeling, and sizing — the event is dropped from the labeled signal
    # stream but persists in the decision record for audit.
    rejection: RejectedEvent | None = None

    # Phase 2.3.1: free-form boolean flags, populated by stages and surfaced
    # in the decision record (e.g., ``{"extended_hours": True}``). Kept narrow
    # — for typed sub-scores use the named fields above.
    flags: dict[str, bool] = Field(default_factory=dict)

    @property
    def print(self) -> OptionsPrint:
        """Alias for the underlying print."""
        return self.print_
