"""Stage Protocol and shared ``PipelineContext`` for cross-stage state."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.providers import (
    MedianTradeSizeProvider,
    NoOpMedianTradeSizeProvider,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


# Cluster-buffer key: same identity as fusion's bucket key but used for the
# Module 38 per-(ticker, strike, expiry, option_type) recent-prints view.
# We keep it as a tuple of plain types (str/str/str/str) so the deque dict
# stays Pydantic-friendly and trivially hashable.
ClusterKey = tuple[str, str, str, str]  # (ticker, strike, expiry, option_type)


def cluster_key(event: EnrichedEvent) -> ClusterKey:
    """Build the cluster-buffer key from an enriched event's print fields."""
    p = event.print_
    return (p.ticker, str(p.strike), p.expiry.isoformat(), p.option_type)


class PipelineContext(BaseModel):
    """Shared mutable state across stages within a single pipeline run.

    The active ``CalibrationProfile`` lives here so every stage reads the
    same numbers; profile hot-swap is handled by the orchestrator at the
    boundary between events, not mid-event.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profile: CalibrationProfile = Field(default_factory=load_default_profile)

    # Injected clock — frozen in tests via ``freezegun`` or a callable override.
    clock: Callable[[], datetime] = Field(default=_utc_now)

    # Flat recent-events buffer (legacy; Phase 1 stages relied on it).
    # Cluster Module 38 now uses ``cluster_buffers`` (per-key) instead.
    recent_events: deque[EnrichedEvent] = Field(
        default_factory=lambda: deque(maxlen=1024),
    )

    # Per-(ticker,strike,expiry,option_type) recent-prints buffer for Module
    # 38. Each deque is bounded; lookups are O(1). Buckets accrete as events
    # arrive; aged-out entries are skipped at scoring time (the stage filters
    # on ``profile.cluster.window_minutes`` rather than evicting eagerly so
    # tests can introspect what was seen).
    cluster_buffers: dict[ClusterKey, deque[EnrichedEvent]] = Field(
        default_factory=dict,
    )

    # Sector mapping (ticker → sector) — Module 25 stub will read from here.
    sector_map: dict[str, str] = Field(default_factory=dict)

    # Module 37 dependency. Defaults to NoOp so the pipeline runs without
    # any reference data wired up; tests inject InMemory; CLI may inject CSV.
    median_trade_size_provider: MedianTradeSizeProvider = Field(
        default_factory=NoOpMedianTradeSizeProvider,
    )

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
