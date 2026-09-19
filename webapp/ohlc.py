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


@dataclass(frozen=True)
class RegularBar:
    """One regular session's open/high/low/close. Phase 5.3.1.

    Any field is ``None`` when that value is missing or non-numeric in the
    payload. A row is never skipped for a missing high or low and a value is
    never guessed: the ATR window that reads these breaks on a gap instead
    (contract phase-5.3 §3.1, §3.2).
    """

    day: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _regular_session_rows(data: object) -> list[tuple[date, dict[str, object]]]:
    """(day, row) for every regular-session row with a parseable date, oldest first.

    Both public readers are built on this, so they cannot drift: the session
    filter, the drop rules and the explicit ordering live in exactly one place
    (contract phase-5.3 §6.7).
    """
    if not isinstance(data, list):
        return []
    out: list[tuple[date, dict[str, object]]] = []
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
        out.append((day, row))
    out.sort(key=lambda pair: pair[0])
    return out


def regular_session_bars(data: object) -> list[RegularBar]:
    """Every regular-session row as an OHLC bar, oldest first.

    Same rows, same order and same drop rules as ``regular_session_closes``;
    this one keeps open/high/low as well. It costs no request: the daily-close
    job already fetches this payload (contract phase-5.3 §3.1).
    """
    return [
        RegularBar(
            day=day,
            open=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
        )
        for day, row in _regular_session_rows(data)
    ]


def regular_session_closes(data: object) -> list[RegularClose]:
    """Every ``market_time == "r"`` row of an ohlc/1d ``data`` list, oldest first.

    Rows that are not objects, belong to another session, or carry no parseable
    ISO ``date`` are dropped (an undated row cannot be ordered). The newest
    entry can be the current day's session while it is still open, so its
    close may be an intraday price rather than a settled close.
    """
    return [
        RegularClose(day=day, close=_to_float(row.get("close")))
        for day, row in _regular_session_rows(data)
    ]
