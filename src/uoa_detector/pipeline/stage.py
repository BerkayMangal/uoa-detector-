"""Stage Protocol and shared ``PipelineContext`` for cross-stage state."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.events import EnrichedEvent


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class PipelineContext(BaseModel):
    """Shared mutable state across stages within a single pipeline run.

    Kept deliberately small in Phase 1. Stages that need richer context (e.g.,
    Module 25 sector mappings, Module 38 cluster history) should pull from here.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: AppConfig = Field(default_factory=default_config)

    # Injected clock — frozen in tests via ``freezegun`` or a callable override.
    clock: Callable[[], datetime] = Field(default=_utc_now)

    # Recent-events buffer for clustering (Module 38). Kept as a deque for O(1)
    # append/pop. Size cap protects against runaway growth in long backtests.
    recent_events: deque[EnrichedEvent] = Field(
        default_factory=lambda: deque(maxlen=1024),
    )

    # Sector mapping (ticker → sector) — Module 25 stub will read from here.
    sector_map: dict[str, str] = Field(default_factory=dict)

    def now(self) -> datetime:
        """Return the current time per the injected clock."""
        return self.clock()


@runtime_checkable
class EnrichmentStage(Protocol):
    """A single enrichment stage in the pipeline.

    Stages mutate ``event`` in place and return it (for fluent chaining).
    Stages MUST be idempotent: running the same stage twice on the same event
    must not change the result.
    """

    name: str

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent: ...
