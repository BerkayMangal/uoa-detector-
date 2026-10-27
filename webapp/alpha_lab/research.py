"""Frozen, descriptive stock counterfactuals. No options pricing or live promotion."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import CalendarSettings
from webapp.alpha_lab.flow import utc
from webapp.ohlc import RegularBar


def following_sessions(day: date, count: int, calendar: CalendarSettings) -> tuple[date, ...]:
    result: list[date] = []
    while len(result) < count:
        day += timedelta(days=1)
        valid = is_session_day(day, calendar)
        if valid is None:
            return ()  # unknown year is not a weekday guess
        if valid:
            result.append(day)
    return tuple(result)


@dataclass(frozen=True)
class StockOutcome:
    state: str
    entry_day: date | None
    exit_day: date | None
    stock_net_pct: float | None = None
    peers_net_pct: float | None = None
    spy_net_pct: float | None = None
    peer_excess_pp: float | None = None
    spy_excess_pp: float | None = None


def outcome(
    signal_day: date,
    ticker: str,
    peers: Sequence[str],
    benchmark: str,
    horizon: int,
    bars: Mapping[str, Sequence[RegularBar]],
    now: datetime,
    calendar: CalendarSettings,
    slippage_bps: float,
) -> StockOutcome:
    from zoneinfo import ZoneInfo

    sessions = following_sessions(signal_day, horizon, calendar)
    if not sessions:
        return StockOutcome("calendar_unknown", None, None)
    entry_day, exit_day = sessions[0], sessions[-1]
    close_time = (
        calendar.early_close if exit_day in calendar.early_closes else calendar.regular_close
    )
    due = datetime.combine(exit_day, close_time, tzinfo=ZoneInfo(calendar.timezone))
    if utc(now) <= due:
        return StockOutcome("pending", entry_day, exit_day)
    returns: dict[str, float] = {}
    slip = slippage_bps / 10000
    if not peers:
        return StockOutcome("unknown_prices", entry_day, exit_day)
    for name in dict.fromkeys((ticker, benchmark, *peers)):
        by_day = {b.day: b for b in bars.get(name, ())}
        first, last = by_day.get(entry_day), by_day.get(exit_day)
        if (
            first is None
            or last is None
            or first.open is None
            or last.close is None
            or not math.isfinite(first.open)
            or not math.isfinite(last.close)
            or first.open <= 0
            or last.close <= 0
        ):
            return StockOutcome("unknown_prices", entry_day, exit_day)
        returns[name] = (last.close * (1 - slip) / (first.open * (1 + slip)) - 1) * 100
        if not math.isfinite(returns[name]):
            return StockOutcome("unknown_prices", entry_day, exit_day)
    peer_return = sum(returns[p] for p in peers) / len(peers)
    return StockOutcome(
        "priced",
        entry_day,
        exit_day,
        returns[ticker],
        peer_return,
        returns[benchmark],
        returns[ticker] - peer_return,
        returns[ticker] - returns[benchmark],
    )
