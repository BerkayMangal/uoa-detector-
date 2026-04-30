"""In-memory backtest store. Schema mirrors Module 29 of the v5 spec.

Outcomes (1h/1d/3d/5d/10d returns, IV change, MFE, MAE) are stored as
``None`` in Phase 1 — they get populated by a later phase that has access
to historical price data.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize

if TYPE_CHECKING:
    from collections.abc import Sequence

OptionType = Literal["call", "put"]


class StoredSignal(BaseModel):
    """One row of the backtest store. Field groups follow Module 29."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    timestamp: datetime
    ticker: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    dte: int

    # Pricing
    premium: Decimal
    option_price: Decimal
    moneyness: Decimal  # spot / strike

    # Sub-scores (v5)
    uoa_score: float | None
    convexity_score: float | None
    event_score: float | None
    gamma_score: float | None
    price_confirmation_score: float | None
    sector_confirmation_score: float | None
    time_of_day_weight: float | None
    cluster_density_score: float | None
    relative_premium_score: float | None
    dte_multiplier_applied: float | None

    # Combined scores
    combined_score_pre_penalty: float | None
    combined_score_post_penalty: float | None
    penalties_applied: list[AppliedPenalty] = Field(default_factory=list)
    contradiction_penalty_applied: bool = False

    # Classification
    sweep_classification: str | None
    is_iso: bool
    gamma_flag: bool  # GAMMA_ACCELERATION_RISK on this event
    sector_confirmation: bool
    dark_pool_confirmation: bool

    # Decision
    label: SignalLabel
    label_reason: str
    max_r: float
    scale_in: bool
    initial_r: float | None

    # Outcomes — populated by a later phase
    spot_return_1h: float | None = None
    spot_return_1d: float | None = None
    spot_return_3d: float | None = None
    spot_return_5d: float | None = None
    spot_return_10d: float | None = None
    iv_change_after_signal: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    final_label: SignalLabel | None = None
    next_day_oi_confirmed: bool | None = None


class BacktestStore:
    """List-backed in-memory store. Phase 1 only — Phase N will swap to Timescale."""

    def __init__(self) -> None:
        self._rows: list[StoredSignal] = []

    def add(
        self,
        event: EnrichedEvent,
        decision: LabelDecision,
        size: PositionSize,
    ) -> StoredSignal:
        """Persist one fully-decided signal. Returns the stored row."""
        pr = event.print_
        moneyness = pr.spot_price / pr.strike if pr.strike > 0 else Decimal(0)

        row = StoredSignal(
            timestamp=pr.timestamp,
            ticker=pr.ticker,
            option_type=pr.option_type,
            strike=pr.strike,
            expiry=pr.expiry,
            dte=pr.dte,
            premium=pr.premium_paid,
            option_price=pr.option_price,
            moneyness=moneyness,
            uoa_score=event.uoa_score,
            convexity_score=event.convexity_score,
            event_score=event.event_score,
            gamma_score=event.gamma_score,
            price_confirmation_score=event.price_confirmation_score,
            sector_confirmation_score=event.sector_confirmation_score,
            time_of_day_weight=event.time_of_day_weight,
            cluster_density_score=event.cluster_density_score,
            relative_premium_score=event.relative_premium_score,
            dte_multiplier_applied=event.dte_multiplier_applied,
            combined_score_pre_penalty=event.combined_score_pre_penalty,
            combined_score_post_penalty=event.combined_score_post_penalty,
            penalties_applied=list(event.applied_penalties),
            contradiction_penalty_applied=event.contradiction_penalty_applied,
            sweep_classification=event.sweep_classification,
            is_iso=pr.is_iso,
            gamma_flag=event.is_gamma_acceleration,
            sector_confirmation=event.has_sector_confirmation,
            dark_pool_confirmation=event.has_dark_pool_confirmation,
            label=decision.label,
            label_reason=decision.reason,
            max_r=size.max_r,
            scale_in=size.scale_in,
            initial_r=size.initial_r,
            next_day_oi_confirmed=event.next_day_oi_confirmed,
        )
        self._rows.append(row)
        return row

    def all(self) -> Sequence[StoredSignal]:
        """Return all stored signals (read-only view)."""
        return tuple(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def to_dataframe(self) -> pd.DataFrame:
        """Return the store contents as a pandas DataFrame for analysis.

        Lists/structs are flattened where practical; ``penalties_applied`` is
        kept as a list-of-dict column.
        """
        records: list[dict[str, object]] = []
        for r in self._rows:
            d = r.model_dump()
            d["penalties_applied"] = [p.model_dump() for p in r.penalties_applied]
            records.append(d)
        return pd.DataFrame.from_records(records)
