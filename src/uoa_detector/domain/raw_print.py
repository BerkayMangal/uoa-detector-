"""``RawPrint`` — per-source, minimally normalized options-flow event.

Each source adapter produces ``RawPrint`` carrying that source's native fields
plus a common subset. The ``SourceFusion`` layer reconciles ``RawPrint``s from
multiple sources into one canonical ``OptionsPrint``.

Compared to ``OptionsPrint``, ``RawPrint``:
  - has no ``SourceAgreement`` (it's a single-source observation)
  - records ``source_id`` directly
  - is allowed to omit fields that are source-specific (open_interest,
    implied_volatility) when that source doesn't supply them
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uoa_detector.domain.events import FillSide, OptionType


class RawPrint(BaseModel):
    """One options print as observed by a single source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Source identity
    source_id: str = Field(description="Stable identifier of the source feed.")
    source_event_id: str = Field(
        description="Source-native event ID; combined with source_id forms a global ID.",
    )

    # Trade identity (used for fusion bucketing)
    timestamp: datetime
    ticker: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    dte: int = Field(ge=0)

    # Pricing
    spot_price: Decimal
    premium_paid: Decimal
    option_price: Decimal
    bid: Decimal
    ask: Decimal
    fill_side: FillSide = "unknown"
    exchange: str

    # Optional fields (some sources don't supply these)
    implied_volatility: float | None = Field(default=None, ge=0.0)
    open_interest: int | None = Field(default=None, ge=0)
    is_iso: bool = False

    # Source-specific tagging from labeled aggregators (e.g., Unusual Whales)
    source_tags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "RawPrint.timestamp must be tz-aware (UTC)"
            raise ValueError(msg)
        return v

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, v: str) -> str:
        if not v.strip():
            msg = "ticker must not be empty"
            raise ValueError(msg)
        return v.strip().upper()

    @property
    def global_event_id(self) -> str:
        """``<source_id>:<source_event_id>`` — globally unique across sources."""
        return f"{self.source_id}:{self.source_event_id}"
