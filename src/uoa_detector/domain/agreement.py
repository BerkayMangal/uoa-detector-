"""``SourceAgreement`` — describes how multiple feeds agreed on a single print.

``ScoreAdjustment`` — additive adjustment to a sub-score (e.g., Module 34's
sweep bonuses). Tracked separately from the underlying score so the decision
record can show "uoa_score 0.65, +0.15 from ISO bonus = 0.80".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfidenceTier = Literal["unanimous", "majority", "single", "conflicted"]


class SourceAgreement(BaseModel):
    """How multiple feeds agreed (or didn't) on one print.

    Single-source mode produces ``confidence_tier="single"`` with one entry
    in ``sources_seen`` and zero disagreement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources_seen: tuple[str, ...] = Field(
        description="Source IDs that reported this print, deterministic order.",
    )
    premium_disagreement: Decimal = Field(
        description="Max minus min ``premium_paid`` across sources (zero if single).",
    )
    timestamp_skew_ms: int = Field(
        ge=0,
        description="Max minus min timestamp in milliseconds across sources.",
    )
    classification_disagreement: bool = Field(
        description="True if sources disagreed on sweep/block/iso classification.",
    )
    confidence_tier: ConfidenceTier
    exchanges_seen: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Distinct exchange codes observed across sources — feeds Module 34.",
    )


def single_source_agreement(source_id: str, exchange: str = "") -> SourceAgreement:
    """Build a ``SourceAgreement`` for a single-source observation."""
    return SourceAgreement(
        sources_seen=(source_id,),
        premium_disagreement=Decimal("0"),
        timestamp_skew_ms=0,
        classification_disagreement=False,
        confidence_tier="single",
        exchanges_seen=(exchange,) if exchange else (),
    )


class ScoreAdjustment(BaseModel):
    """An additive adjustment to a named sub-score.

    Module 34's sweep bonuses (e.g., +0.15 to ``uoa_score`` for ISO) flow
    through this rather than mutating the underlying score directly. The
    scoring engine sums adjustments per target field at score time, so the
    decision record can show provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(description="Sub-score field name (e.g., 'uoa_score').")
    delta: float = Field(description="Additive adjustment, typically positive.")
    reason: str = Field(description="Human-readable provenance for the decision record.")
    source_module: str = Field(
        description="Module name that produced the adjustment (e.g., 'm34_sweep_block').",
    )
