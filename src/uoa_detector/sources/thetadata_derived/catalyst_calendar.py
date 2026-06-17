"""CSV-backed catalyst calendar provider (Phase 3.6.6) for Module 22.

Reads the committed earnings calendar (``scripts/fetch_earnings_calendar.py``
→ ``data/earnings_calendar.csv``) and serves the ``CatalystCalendarProvider``
Protocol M22 consumes — so the event-calendar axis (a *weighted*
combined-score axis) runs without Unusual Whales.

CSV schema: ``ticker, when (ISO datetime UTC), kind, title``.
"""

from __future__ import annotations

import bisect
import csv
from datetime import datetime
from typing import TYPE_CHECKING, cast

from uoa_detector.providers.catalyst_calendar import CatalystEvent

if TYPE_CHECKING:
    from pathlib import Path

    from uoa_detector.providers.catalyst_calendar import CatalystKind


class CSVCatalystCalendarProvider:
    """``CatalystCalendarProvider`` backed by the earnings-calendar CSV."""

    def __init__(self, csv_path: Path) -> None:
        # ticker -> (sorted whens, parallel events)
        self._by_ticker: dict[str, tuple[list[datetime], list[CatalystEvent]]] = {}
        self._load(csv_path)

    def _load(self, csv_path: Path) -> None:
        raw: dict[str, list[CatalystEvent]] = {}
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ticker = row["ticker"].strip().upper()
                event = CatalystEvent(
                    ticker=ticker,
                    kind=cast("CatalystKind", row["kind"].strip()),
                    when=datetime.fromisoformat(row["when"]),
                    title=row["title"],
                )
                raw.setdefault(ticker, []).append(event)
        for ticker, events in raw.items():
            events.sort(key=lambda e: e.when)
            self._by_ticker[ticker] = ([e.when for e in events], events)

    async def next_catalyst(
        self, ticker: str, after: datetime,
    ) -> CatalystEvent | None:
        entry = self._by_ticker.get(ticker.upper())
        if entry is None:
            return None
        whens, events = entry
        pos = bisect.bisect_left(whens, after)
        if pos >= len(events):
            return None
        return events[pos]

    async def catalysts_in_window(
        self, ticker: str, window_start: datetime, window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        entry = self._by_ticker.get(ticker.upper())
        if entry is None:
            return ()
        whens, events = entry
        lo = bisect.bisect_left(whens, window_start)
        hi = bisect.bisect_right(whens, window_end)
        return tuple(events[lo:hi])
