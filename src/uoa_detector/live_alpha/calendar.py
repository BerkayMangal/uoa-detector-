"""The US equity session clock for Live Alpha (contract §4).

``sources.market_hours.is_market_open`` is a weekday plus a fixed UTC window and
says so: no holidays, no early closes. That is fine for poll throttling and wrong
for a recommendation, where "the market is open" decides whether a quote is
executable. This module converts to ET with ``zoneinfo`` (so DST is exact) and
reads the NYSE holidays and 13:00 early closes from the profile. A date in a year
the profile does not list is DEGRADED — it never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.settings import CalendarSettings


class MarketMode(StrEnum):
    LIVE = "LIVE"
    PREMARKET = "PREMARKET"
    CLOSED = "CLOSED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class Session:
    """Where ``moment`` falls in the trading calendar."""

    mode: MarketMode
    et_now: datetime
    session_date: date | None      # today's session, or None when today has none
    close_et: time | None          # 16:00 or the early close; None without a session
    next_session: date | None      # the next session date strictly after today
    reason: str                    # why DEGRADED/CLOSED, empty when LIVE


def _zone(cal: CalendarSettings) -> ZoneInfo:
    return ZoneInfo(cal.timezone)


def is_session_day(day: date, cal: CalendarSettings) -> bool | None:
    """True/False for a listed year; None for a year the profile does not cover."""
    if day.year not in cal.years:
        return None
    return day.weekday() < 5 and day not in cal.holidays


def close_time(day: date, cal: CalendarSettings) -> time:
    return cal.early_close if day in cal.early_closes else cal.regular_close


def next_session_after(day: date, cal: CalendarSettings) -> date | None:
    probe = day
    for _ in range(15):
        probe += timedelta(days=1)
        known = is_session_day(probe, cal)
        if known is None:
            return None
        if known:
            return probe
    return None


def previous_session_before(day: date, cal: CalendarSettings) -> date | None:
    probe = day
    for _ in range(15):
        probe -= timedelta(days=1)
        known = is_session_day(probe, cal)
        if known is None:
            return None
        if known:
            return probe
    return None


def classify(moment: datetime, cal: CalendarSettings) -> Session:
    """The session state at ``moment`` (timezone-aware)."""
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    et = moment.astimezone(_zone(cal))
    today = et.date()
    known = is_session_day(today, cal)
    if known is None:
        return Session(
            mode=MarketMode.DEGRADED, et_now=et, session_date=None, close_et=None,
            next_session=None, reason="takvim bilinmiyor",
        )
    nxt = next_session_after(today, cal)
    if not known:
        return Session(
            mode=MarketMode.CLOSED, et_now=et, session_date=None, close_et=None,
            next_session=nxt, reason="tatil" if today.weekday() < 5 else "hafta sonu",
        )
    close = close_time(today, cal)
    clock = et.time()
    if clock < cal.premarket_open:
        mode, reason = MarketMode.CLOSED, "seans öncesi (gece)"
    elif clock < cal.regular_open:
        mode, reason = MarketMode.PREMARKET, "açılış öncesi"
    elif clock < close:
        mode, reason = MarketMode.LIVE, ""
    else:
        mode, reason = MarketMode.CLOSED, "seans kapandı"
    return Session(
        mode=mode, et_now=et, session_date=today, close_et=close, next_session=nxt, reason=reason,
    )


def is_live(moment: datetime, cal: CalendarSettings) -> bool:
    return classify(moment, cal).mode is MarketMode.LIVE


def sessions_between(start: date, end: date, cal: CalendarSettings) -> int | None:
    """Session days in ``(start, end]``; None when a year is not covered."""
    count = 0
    probe = start
    while probe < end:
        probe += timedelta(days=1)
        known = is_session_day(probe, cal)
        if known is None:
            return None
        if known:
            count += 1
    return count


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)
