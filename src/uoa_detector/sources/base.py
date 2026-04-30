"""``FlowDataSource`` Protocol — the only interface stages depend on.

Add a new feed (Polygon, Unusual Whales, IBKR, CSV replay, …) by implementing
this Protocol. Nothing else in the system needs to change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from uoa_detector.domain.events import OptionsPrint


@runtime_checkable
class FlowDataSource(Protocol):
    """Pluggable options-flow feed.

    Implementations: synthetic, polygon, unusual_whales, csv_replay, ...
    """

    def stream(self) -> AsyncIterator[OptionsPrint]:
        """Yield normalized ``OptionsPrint`` events as they arrive.

        Implementations should be ``async def`` generators; the return type is the
        ``AsyncIterator`` they expose. Errors must be raised as ``DataSourceError``
        (or a subclass) — never as bare ``Exception``.
        """
        ...

    async def close(self) -> None:
        """Release any underlying connections.

        Idempotent — safe to call multiple times.
        """
        ...
