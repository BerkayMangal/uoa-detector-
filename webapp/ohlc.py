"""Regular-session closes from the UW ``/api/stock/{t}/ohlc/1d`` payload.

Live probe (SPY, 2026-09-14): ``{"data": [...]}`` holds about 752 rows,
NEWEST FIRST, with several rows per ``date`` told apart by ``market_time``:
``"pr"`` (pre-market), ``"r"`` (regular session), ``"po"`` (post-market).
Values are strings (``"close": "762.04"``).

Consumers (journal pricing, vol-board realized vol) must neither mix sessions
nor rely on the API's row order, so this module keeps regular-session rows only
and orders them by ``date`` explicitly (contract phase-5.0-merge §3.9).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

_REGULAR_SESSION = "r"


@dataclass(frozen=True)
class RegularClose:
    day: date
    close: float | None  # None when the row's close is missing or non-numeric


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def regular_session_closes(data: object) -> list[RegularClose]:
    """Every ``market_time == "r"`` row of an ohlc/1d ``data`` list, oldest first.

    Rows that are not objects, belong to another session, or carry no parseable
    ISO ``date`` are dropped (an undated row cannot be ordered). The newest
    entry can be the current day's session while it is still open, so its
    close may be an intraday price rather than a settled close.
    """
    if not isinstance(data, list):
        return []
    out: list[RegularClose] = []
    for row in data:
        if not isinstance(row, dict) or row.get("market_time") != _REGULAR_SESSION:
            continue
        raw_day = row.get("date")
        if not isinstance(raw_day, str):
            continue
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        out.append(RegularClose(day=day, close=_to_float(row.get("close"))))
    out.sort(key=lambda bar: bar.day)
    return out
