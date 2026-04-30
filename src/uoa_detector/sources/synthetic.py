"""Scriptable in-memory flow sources for tests and the CLI smoke run.

Phase 2.3.4 retired the ``SyntheticFlowSource`` Phase-1 class (which emitted
canonical ``OptionsPrint``s pre-fusion). The pipeline now consumes
``RawFlowSource``s through ``SourceFusion``, so this module exposes:

  - ``SyntheticRawFlowSource``: in-memory source emitting scripted
    ``RawPrint`` events through the ``RawFlowSource`` Protocol.
  - ``StallingRawFlowSource``: test-only — emits N events then parks
    forever on an ``asyncio.Event`` until ``close()`` releases it.
    Used by the stalled-source recovery test.
  - ``to_raw_print``: helper that converts a canonical ``OptionsPrint``
    into a ``RawPrint`` with a chosen ``source_id``. Used by Phase-1
    scenarios that authored prints as ``OptionsPrint`` directly; new
    scenarios should construct ``RawPrint`` directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint


def to_raw_print(p: OptionsPrint, source_id: str = "synthetic") -> RawPrint:
    """Convert a canonical ``OptionsPrint`` into a ``RawPrint``.

    Used to feed Phase 1-style scenarios (which authored prints as
    ``OptionsPrint``) through the Phase 2 ``SourceFusion`` path. The
    conversion is field-for-field; ``source_event_id`` inherits the
    ``OptionsPrint.event_id`` so single-source fusion preserves event-id
    continuity for downstream systems (scenario overrides keyed by
    ``event_id``, decision-record correlation, etc.).
    """
    return RawPrint(
        source_id=source_id,
        source_event_id=p.event_id,
        timestamp=p.timestamp,
        ticker=p.ticker,
        option_type=p.option_type,
        strike=p.strike,
        expiry=p.expiry,
        dte=p.dte,
        spot_price=p.spot_price,
        premium_paid=p.premium_paid,
        option_price=p.option_price,
        bid=p.bid,
        ask=p.ask,
        fill_side=p.fill_side,
        exchange=p.exchange,
        is_iso=p.is_iso,
        implied_volatility=p.implied_volatility,
        open_interest=p.open_interest,
    )


class SyntheticRawFlowSource:
    """In-memory ``RawFlowSource`` — emits scripted ``RawPrint`` events.

    Used to drive ``SourceFusion`` in tests and the CLI. The ``source_id``
    field is publicly readable so ``SourceFusion`` can identify the feed.

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
