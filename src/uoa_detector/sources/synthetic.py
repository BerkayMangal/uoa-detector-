"""Scriptable in-memory flow sources for tests and the CLI smoke run.

  - ``SyntheticFlowSource`` (Phase 1): emits canonical ``OptionsPrint`` events.
    Retained for back-compat with the Phase 1 integration test harness; will
    be migrated to a thin shim over ``SyntheticRawFlowSource`` + ``SourceFusion``
    in Phase 2.3.4.

  - ``SyntheticRawFlowSource`` (Phase 2.3.3): emits ``RawPrint`` events
    through the ``RawFlowSource`` Protocol. Used to drive ``SourceFusion``
    in tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint


class SyntheticFlowSource:
    """In-memory ``FlowDataSource`` (Phase 1 legacy — emits ``OptionsPrint``).

    Initialise with a sequence of ``OptionsPrint`` objects; ``stream`` yields
    them in order with optional ``inter_event_delay`` seconds between each.
    Implements the ``FlowDataSource`` Protocol structurally — no inheritance.
    """

    def __init__(
        self,
        events: Iterable[OptionsPrint],
        *,
        inter_event_delay: float = 0.0,
    ) -> None:
        self._events: list[OptionsPrint] = list(events)
        self._inter_event_delay = inter_event_delay
        self._closed = False

    async def stream(self) -> AsyncIterator[OptionsPrint]:
        """Yield each scripted event in order."""
        for ev in self._events:
            if self._closed:
                return
            if self._inter_event_delay > 0:
                await asyncio.sleep(self._inter_event_delay)
            yield ev

    async def close(self) -> None:
        """Mark the source as closed; subsequent ``stream`` calls produce nothing."""
        self._closed = True


class SyntheticRawFlowSource:
    """In-memory ``RawFlowSource`` — emits scripted ``RawPrint`` events.

    Used to drive ``SourceFusion`` in tests. The ``source_id`` field is
    publicly readable so ``SourceFusion`` can identify the feed.

    ``inter_event_delay`` (seconds, walltime) lets tests space events out so
    asyncio can interleave with stall checks. Default 0 = emit as fast as
    possible.
    """

    def __init__(
        self,
        source_id: str,
        events: Iterable[RawPrint],
        *,
        inter_event_delay: float = 0.0,
    ) -> None:
        self.source_id = source_id
        self._events: list[RawPrint] = list(events)
        self._inter_event_delay = inter_event_delay
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield each scripted event in order."""
        for ev in self._events:
            if self._closed:
                return
            if self._inter_event_delay > 0:
                await asyncio.sleep(self._inter_event_delay)
            yield ev

    async def close(self) -> None:
        """Mark the source as closed; subsequent ``stream`` calls produce nothing."""
        self._closed = True


class StallingRawFlowSource:
    """Test-only ``RawFlowSource`` that emits some events then stalls forever.

    After yielding all scripted events, ``stream`` waits on an ``asyncio.Event``
    that is only set when ``close()`` is called. This simulates a source that
    has gone silent (or died) without ending its stream — the production
    failure mode that motivates ``SourceFusion``'s stalled-source timeout.

    Phase 2.3.3 only — used by ``test_stalled_source_does_not_block_fusion``.
    Real sources should not stall silently; they should raise
    ``DataSourceError`` or end their stream cleanly.
    """

    def __init__(self, source_id: str, events: Iterable[RawPrint]) -> None:
        self.source_id = source_id
        self._events: list[RawPrint] = list(events)
        self._stop_event: asyncio.Event | None = None
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield each scripted event, then await ``close()`` indefinitely."""
        # Lazily create the asyncio.Event inside the running loop.
        self._stop_event = asyncio.Event()
        for ev in self._events:
            if self._closed:
                return
            yield ev
        # Stall: wait for close().
        await self._stop_event.wait()

    async def close(self) -> None:
        """Release the stall and mark closed."""
        self._closed = True
        if self._stop_event is not None:
            self._stop_event.set()
