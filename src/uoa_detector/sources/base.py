"""Source Protocols.

Three structurally-distinct interfaces:

  - ``FlowDataSource`` — Phase 1 legacy: streams pre-canonical ``OptionsPrint``
    events. Will become a thin shim over ``RawFlowSource`` + ``SourceFusion``
    once the migration in Phase 2.3.4 lands. New adapters should NOT implement
    this directly.

  - ``RawFlowSource`` — Phase 2 multi-source path: streams ``RawPrint`` events
    that carry source-specific fields. ``SourceFusion`` reconciles outputs
    from one or more ``RawFlowSource`` instances into canonical ``OptionsPrint``s.

  - ``QuoteSnapshotSource`` — Phase 2 quote/IV/OI lookup: NOT a stream. A
    point-in-time interface for IBKR-style data needed by enrichment stages
    (Module 24 IV exhaustion, Module 36 OI penalty, etc.).

source_id naming convention (free-form string, not enforced — tests and
adapters self-police): lowercase, no spaces, descriptive. Examples:
``polygon``, ``unusual_whales``, ``ibkr``, ``synthetic``,
``synthetic_a`` / ``synthetic_b`` for multi-source tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint


@runtime_checkable
class FlowDataSource(Protocol):
    """Phase 1 legacy Protocol — emits canonical ``OptionsPrint`` events.

    Retained for back-compat with the Phase 1 integration test harness and
    the existing ``SyntheticFlowSource``. Phase 2.3.4 migrates the synthetic
    source to ``RawFlowSource`` + ``SourceFusion``; this Protocol stays only
    until adapters that legitimately deliver canonical prints (e.g., a
    parquet replay of fused output) need it.

    New adapters should implement ``RawFlowSource`` instead.
    """

    def stream(self) -> AsyncIterator[OptionsPrint]:
        """Yield normalized ``OptionsPrint`` events as they arrive."""
        ...

    async def close(self) -> None:
        """Release any underlying connections. Idempotent."""
        ...


@runtime_checkable
class RawFlowSource(Protocol):
    """Phase 2 Protocol — emits per-source ``RawPrint`` events for fusion.

    Implementations preserve their feed's native fields on ``RawPrint`` and
    set ``source_id`` to a stable lowercase identifier (see naming convention
    in this module's docstring). ``SourceFusion`` reconciles the streams from
    one or more ``RawFlowSource`` instances into canonical ``OptionsPrint``s.

    Single-source mode is supported: a pipeline with one ``RawFlowSource`` and
    fusion in single-source mode produces ``OptionsPrint``s with
    ``source_agreement.confidence_tier == "single"`` immediately, with no
    windowing wait.
    """

    source_id: str

    def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint`` events as they arrive from this feed."""
        ...

    async def close(self) -> None:
        """Release any underlying connections. Idempotent."""
        ...


class QuoteSnapshot(BaseModel):
    """Point-in-time quote / IV / OI snapshot for one option.

    Returned by ``QuoteSnapshotSource.snapshot``. Some fields are optional —
    not every venue exposes IV or OI. Stages that need a missing field should
    fall back to a ``Provider`` (Phase 3) rather than crash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    as_of: datetime  # tz-aware UTC; the wall-clock time the snapshot is valid for

    ticker: str
    strike: Decimal
    expiry: date
    option_type: Literal["call", "put"]

    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    implied_volatility: float | None = Field(default=None, ge=0.0)
    open_interest: int | None = Field(default=None, ge=0)

    @field_validator("as_of")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "QuoteSnapshot.as_of must be tz-aware (UTC)"
            raise ValueError(msg)
        return v


@runtime_checkable
class QuoteSnapshotSource(Protocol):
    """Phase 2 Protocol — point-in-time quote / IV / OI lookup.

    Distinct from ``RawFlowSource``: NOT a stream. Stages call ``snapshot()``
    when they need the bid/ask/IV/OI for a specific option at a specific time
    (e.g., Module 24's IV-exhaustion check). Implementations: ``ibkr_quotes``
    (Phase 3), in-memory/CSV stubs for tests.
    """

    source_id: str

    async def snapshot(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> QuoteSnapshot:
        """Return a snapshot valid for ``at`` (tz-aware UTC)."""
        ...

    async def close(self) -> None:
        """Release any underlying connections. Idempotent."""
        ...
