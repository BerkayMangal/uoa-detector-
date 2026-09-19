"""The spot decision frame: entry, stop, size, risked dollars, target, R.

Contract: ``docs/phase-5.3-spot-frame-acceptance.md`` §3.2 and the binding rules
R-SP1 … R-SP8 of §4.

Pure, like ``moves.py`` and ``sizing.py``: no I/O, no Unusual Whales call, no
profile read of its own. The caller passes the settings and the stored bars.

Why this module exists at all: every action cell on the board today prices an
*option* — the tradability chip reports the option's spread, the size cell counts
lots of premium, B2 states the move the option needs to break even. The owner
buys and sells **shares**. These are the same facts in the units he trades in.

The honesty rules are not decoration here. A guessed stop is a number the owner
would size a real position against, so every function below returns ``None``
rather than a fallback when its input is missing, and the row states why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from webapp.board.settings import SizingSettings, SpotSettings


@dataclass(frozen=True)
class Bar:
    """One regular session's OHLC, as stored in ``alfa_daily_bar``.

    Any field may be ``None``: a row whose high or low did not parse is stored
    with nulls rather than skipped or guessed (5.3.1), and the True Range window
    below breaks on such a session instead of spanning it.
    """

    high: float | None
    low: float | None
    close: float | None


@dataclass(frozen=True)
class SpotFrame:
    """What the row states. Every optional field is ``None`` for a stated reason."""

    entry: float | None
    atr: float | None
    stop: float | None
    stop_distance: float | None
    stop_pct: float | None          # distance as a percent of entry
    shares: int | None
    risked_usd: float | None        # shares x stop_distance, at or below risk_usd
    risk_usd: float | None          # the owner's R (sizing.r_usd), the intended risk
    target: float | None            # from the ATM straddle; nothing new is derived
    r_to_target: float | None
    sessions_used: int              # usable True Ranges behind the ATR
    capped_by_position_limit: bool  # R-SP6: disclosed, never silent
    reason: str | None              # why a cell is unknown (R-SP1, R-SP8)
    # Where the entry came from. A live quote states nothing extra; a completed
    # session's close is a different kind of fact and the row has to say so, or the
    # reader cannot tell a current price from Friday's (5.3.7).
    entry_as_of: date | None = None
    entry_is_close: bool = False


def true_ranges(bars: Sequence[Bar]) -> list[float | None]:
    """``max(high-low, |high-prev_close|, |low-prev_close|)`` per session, oldest first.

    A session missing any of the three inputs yields ``None``, which breaks the
    ATR window rather than silently spanning the gap (§3.2).
    """
    out: list[float | None] = []
    previous: float | None = None
    for bar in bars:
        if bar.high is None or bar.low is None or previous is None:
            out.append(None)
        else:
            out.append(max(
                bar.high - bar.low,
                abs(bar.high - previous),
                abs(bar.low - previous),
            ))
        previous = bar.close
    return out


def wilder_atr(ranges: Sequence[float | None], period: int) -> float | None:
    """Wilder's smoothing over the most recent unbroken run of ``period`` ranges.

    A ``None`` anywhere resets the run: the average must not span a session whose
    range could not be computed. Returns ``None`` when no unbroken run is long
    enough.
    """
    if period <= 0:
        return None
    run: list[float] = []
    best: list[float] | None = None
    for value in ranges:
        if value is None or not math.isfinite(value):
            run = []
            continue
        run.append(value)
        if len(run) >= period:
            best = list(run)
    if best is None:
        return None
    atr = sum(best[:period]) / period
    for value in best[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def build_spot_frame(
    bars: Sequence[Bar],
    *,
    entry: float | None,
    direction: str,
    spot: SpotSettings,
    sizing: SizingSettings,
    target: float | None = None,
    entry_is_stale: bool = False,
    entry_as_of: date | None = None,
    entry_is_close: bool = False,
) -> SpotFrame:
    """The frame for one row. ``direction`` is ``"up"`` (long) or anything else (short).

    R-SP8 first: a stale entry kills the whole frame, because sizing a real
    position off a price that no longer exists is worse than showing nothing.
    """
    usable = [r for r in true_ranges(bars) if r is not None]
    empty = SpotFrame(
        entry=None, atr=None, stop=None, stop_distance=None, stop_pct=None,
        shares=None, risked_usd=None, risk_usd=None, target=None, r_to_target=None,
        sessions_used=len(usable), capped_by_position_limit=False, reason=None,
    )

    if entry_is_stale or entry is None or entry <= 0:
        return SpotFrame(**{**empty.__dict__, "reason": REASON_STALE_SPOT})

    atr = wilder_atr(true_ranges(bars), spot.atr_period)
    if atr is None or len(usable) < spot.atr_min_sessions:
        # R-SP1: no bars, no stop — and the row says which of the two it is.
        # The entry survives this refusal, so its provenance must survive with it:
        # a bare price with no date reads as a live quote (5.3.7).
        return SpotFrame(**{
            **empty.__dict__, "entry": entry, "reason": REASON_NOT_ENOUGH_BARS,
            "entry_as_of": entry_as_of, "entry_is_close": entry_is_close,
        })

    distance = spot.atr_stop_multiple * atr
    stop = entry - distance if direction == "up" else entry + distance
    if distance <= 0:
        # R-SP2: no stop, no size. Never a fallback percentage.
        #
        # Only a zero or negative DISTANCE is a missing stop. A long whose stop
        # lands at or below zero is a different fact: the ATR is wider than the
        # share price, so one share risks more than the account allows. That row
        # must still render, with a share count of 0 (R-SP3) — suppressing it
        # would hide a candidate for being volatile, which is the mistake the
        # option gate already makes and O2 exists to stop.
        return SpotFrame(**{
            **empty.__dict__, "entry": entry, "atr": atr, "reason": REASON_NO_STOP_DISTANCE,
            "entry_as_of": entry_as_of, "entry_is_close": entry_is_close,
        })

    # The intended dollar risk is sizing.r_usd itself — the owner's R. Writing it
    # as capital x (r_usd / capital) would cancel to the same number while
    # implying a percentage the profile does not carry (O3: $10,000 and $100).
    risk_usd = sizing.r_usd
    shares = int(risk_usd // distance)  # R-SP3: zero is a fact, and is rendered
    capped = False
    limit = sizing.capital_usd * spot.max_position_pct_of_capital / 100.0
    if shares * entry > limit:
        shares = int(limit // entry)
        capped = True  # R-SP6: disclosed by the row, never silent

    r_to_target = (target - entry) / distance if target is not None else None
    return SpotFrame(
        entry=entry,
        atr=atr,
        stop=stop,
        stop_distance=distance,
        stop_pct=distance / entry * 100.0,
        shares=shares,
        risked_usd=shares * distance,
        risk_usd=risk_usd,
        target=target,
        r_to_target=r_to_target,
        sessions_used=len(usable),
        capped_by_position_limit=capped,
        reason=None,
        entry_as_of=entry_as_of,
        entry_is_close=entry_is_close,
    )


# Reasons are copy keys, resolved by the page layer (5.3.3), not sentences here:
# this module stays free of UI text exactly as moves.py keeps its arithmetic
# separate from move_sentence().
REASON_NOT_ENOUGH_BARS = "not_enough_bars"
REASON_NO_STOP_DISTANCE = "no_stop_distance"
REASON_STALE_SPOT = "stale_spot"
