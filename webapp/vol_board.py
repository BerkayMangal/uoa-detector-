"""Pure assembly of the vol-premium board (Phase 4.34).

Ranks the live gamma_regime names by IV-rank (richness of vol to sell) — NOT by
the long-gamma+high-IV "sell cell" (Study D showed that conditioning is weak
OOS, docs/study_D_result.md). Names with earnings inside the structure window
are demoted and flagged: high IV before earnings is a justified charge, not free
premium (the classic vol-selling trap). No DB, no HTTP — fully unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from webapp.gamma import GammaContext


@dataclass(frozen=True)
class VolBoardRow:
    ticker: str
    iv_rank: int | None              # iv_pct * 100, rounded; None if unknown
    atm_iv: float | None
    expected_move_pct: float | None  # ± over window_days, percent
    regime: str                      # "long" | "short" (context only)
    regime_label: str
    call_wall: float | None
    put_wall: float | None
    earnings_in_window: bool
    earnings_date: date | None
    below_threshold: bool            # iv_pct < rich_threshold
    as_of: str                       # gamma snapshot date (freshness)
    realized_vol: float | None       # annualised ~21d realised vol
    vrp_pct: float | None            # implied − realized, vol points (the premium)


def _expected_move_pct(atm_iv: float | None, window_days: int) -> float | None:
    if atm_iv is None:
        return None
    return round(atm_iv * math.sqrt(window_days / 365) * 100, 1)


def build_vol_board(
    contexts: dict[str, GammaContext],
    *,
    earnings: dict[str, date | None],
    now: date,
    window_days: int = 30,
    rich_threshold: float = 0.75,
) -> list[VolBoardRow]:
    """Order names for the board: clean (no earnings in window) first, then
    earnings names; within each group, IV-rank descending (None last)."""
    rows: list[VolBoardRow] = []
    for ticker, ctx in contexts.items():
        edate = earnings.get(ticker)
        in_window = edate is not None and now <= edate <= _add_days(now, window_days)
        iv_rank = None if ctx.iv_pct is None else round(ctx.iv_pct * 100)
        rows.append(VolBoardRow(
            ticker=ticker,
            iv_rank=iv_rank,
            atm_iv=ctx.atm_iv,
            expected_move_pct=_expected_move_pct(ctx.atm_iv, window_days),
            regime=ctx.regime,
            regime_label=ctx.regime_label,
            call_wall=ctx.call_wall,
            put_wall=ctx.put_wall,
            earnings_in_window=in_window,
            earnings_date=edate,
            below_threshold=(ctx.iv_pct is None or ctx.iv_pct < rich_threshold),
            as_of=ctx.as_of,
            realized_vol=ctx.realized_vol,
            vrp_pct=ctx.vrp_pct,
        ))
    # Sort key: clean before earnings; then IV-rank desc (None -> -1, sorts last).
    rows.sort(key=lambda r: (r.earnings_in_window, -(r.iv_rank if r.iv_rank is not None else -1)))
    return rows


def vol_board_summary(rows: list[VolBoardRow]) -> str:
    """One-line descriptive triage of the board (NOT a call). Empty for no rows."""
    if not rows:
        return ""
    total = len(rows)
    rich_clean = sum(1 for r in rows if not r.below_threshold and not r.earnings_in_window)
    earnings_n = sum(1 for r in rows if r.earnings_in_window)
    longs = sum(1 for r in rows if r.regime == "long")
    parts = [
        f"{total} name{'s' if total != 1 else ''}",
        f"{rich_clean} rich-vol clean (IV-rank ≥75)",
    ]
    if earnings_n:
        parts.append(f"{earnings_n} with earnings in window")
    parts.append(f"regime: {longs} long / {total - longs} short")
    return " · ".join(parts)


def _add_days(d: date, n: int) -> date:
    return date.fromordinal(d.toordinal() + n)
