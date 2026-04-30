"""Scriptable in-memory flow source for tests and the CLI smoke run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from uoa_detector.domain.events import OptionsPrint


class SyntheticFlowSource:
    """In-memory flow source.

    Initialise with a sequence of ``OptionsPrint`` objects; ``stream`` yields them
    in order with optional ``inter_event_delay`` seconds between each.

    Implements the ``FlowDataSource`` protocol structurally — no inheritance.
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
