"""``CatalystCalendarProvider`` Protocol — feeds Module 22 (Event Calendar).

Module 22 produces ``event_score`` based on proximity to a known catalyst:
earnings, FDA decisions, Fed meetings, M&A close dates, large index
rebalances. Sources in Phase 3:

  - Earnings: any of Polygon, Benzinga, Earnings Whisper, Refinitiv.
  - FDA: FDA's own calendars, BiopharmCatalyst.
  - Fed: FOMC meeting calendar (static, public).
  - M&A: structured news feeds.

The Protocol surfaces a uniform ``next_catalyst(ticker, after)`` returning
the next scheduled event for ``ticker`` after ``after`` (plus ``None`` if
the calendar is empty for that ticker through some lookahead horizon
the implementation chooses).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

CatalystKind = Literal[
    "earnings",
    "fda",
    "fomc",
    "ma_close",
    "guidance",
    "investor_day",
    "rebalance",
    "other",
]


class CatalystEvent(BaseModel):
    """A scheduled catalyst on the calendar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    kind: CatalystKind
    when: datetime  # tz-aware UTC; some kinds (FOMC) may be wall-clock-time-precise
    title: str  # human-readable; stored on the decision record for review


@runtime_checkable
class CatalystCalendarProvider(Protocol):
    """Source of upcoming catalyst events per ticker."""

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        """Return the next catalyst on or after ``after``; ``None`` if none."""
        ...

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        """Return all catalysts for ``ticker`` with ``when`` in [start, end].

        Phase 3.4.2: feeds M22 (Event calendar score). The window
        spans both past and future relative to the event timestamp
        (acceptance doc: ±30 days). Returns events sorted ascending
        by ``when``. Empty tuple when the calendar has no events
        in the window — Module 22 treats empty as 'neutral fallback'
        (score = no_catalyst_neutral_score), NOT zero.
        """
        ...


class NoOpCatalystCalendarProvider:
    """Always returns ``None`` / empty tuple. Module 22 falls back to neutral."""

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        del ticker, after
        return None

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        del ticker, window_start, window_end
        return ()
