"""Signal labels from Part 4 of the v5 spec — all 17 labels enumerated, plus
the Phase 2.3.2a addition of ``REJECTED`` for events explicitly dropped by
policy (see ``RejectedEvent``).

Verbatim from the spec:
    IGNORE_NOISE, LIKELY_CLOSING_OR_NOISE, POST_EVENT_NOISE, PENALIZED_BELOW_THRESHOLD,
    CONVEXITY_WATCH, OPENING_UNCONFIRMED, CONVEXITY_CLUSTER, CONVEXITY_BURST,
    SECTOR_FLOW_CLUSTER, STANDARD_UOA, SWEEP_UOA, PRE_CATALYST_FLOW,
    CONFIRMED_OPENING_FLOW, OPTIONS_EQUITY_TAPE_CONFIRMATION,
    GAMMA_ACCELERATION_RISK, LEAP_POSITIONING, HIGH_CONVICTION_SEQUENCE

Phase 2.3.2a adds:
    REJECTED — event was not evaluated; out of scope by policy. Distinct from
    IGNORE_NOISE (= evaluated, no signal). For backtest analysis these must be
    separable: IGNORE_NOISE rate measures filter quality, REJECTED rate
    measures policy coverage.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SignalLabel(StrEnum):
    """All 17 v5 spec labels plus Phase 2.3.2a's ``REJECTED``."""

    # Spec labels (Part 4 of v5)
    IGNORE_NOISE = "IGNORE_NOISE"
    LIKELY_CLOSING_OR_NOISE = "LIKELY_CLOSING_OR_NOISE"
    POST_EVENT_NOISE = "POST_EVENT_NOISE"
    PENALIZED_BELOW_THRESHOLD = "PENALIZED_BELOW_THRESHOLD"
    CONVEXITY_WATCH = "CONVEXITY_WATCH"
    OPENING_UNCONFIRMED = "OPENING_UNCONFIRMED"
    CONVEXITY_CLUSTER = "CONVEXITY_CLUSTER"
    CONVEXITY_BURST = "CONVEXITY_BURST"
    SECTOR_FLOW_CLUSTER = "SECTOR_FLOW_CLUSTER"
    STANDARD_UOA = "STANDARD_UOA"
    SWEEP_UOA = "SWEEP_UOA"
    PRE_CATALYST_FLOW = "PRE_CATALYST_FLOW"
    CONFIRMED_OPENING_FLOW = "CONFIRMED_OPENING_FLOW"
    OPTIONS_EQUITY_TAPE_CONFIRMATION = "OPTIONS_EQUITY_TAPE_CONFIRMATION"
    GAMMA_ACCELERATION_RISK = "GAMMA_ACCELERATION_RISK"
    LEAP_POSITIONING = "LEAP_POSITIONING"
    HIGH_CONVICTION_SEQUENCE = "HIGH_CONVICTION_SEQUENCE"

    # Phase 2.3.2a additions
    REJECTED = "REJECTED"


class LabelDecision(BaseModel):
    """Output of the labeler: a label and a brief human-readable reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: SignalLabel
    reason: str
