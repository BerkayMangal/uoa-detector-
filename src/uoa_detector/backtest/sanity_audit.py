"""Phase 3.5.7 — sanity-audit tooling.

Pure functions that turn a run's realized trades (and the raw bid/ask
spreads of the traded universe) into the audit checks pinned in the
Phase 3.5 acceptance doc:

  - **Look-ahead leakage** — every closed trade must exit strictly
    after it enters, **and** must be priced off a quote inside its own
    life and on its own exit session. A closed trade with
    ``exit_ts <= entry_ts`` (or a missing ``exit_ts``) is a time-travel
    bug; so is one whose ``exit_quote_ts`` predates entry or belongs to
    another day. A trade carrying no ``exit_quote_ts`` is counted as
    *unverifiable*, never as clean. (Entry is at the signal timestamp,
    so ``event_ts == entry_ts``; the "no provider data after event_ts"
    half of the contract is enforced upstream by the as-of providers +
    event-time discipline (D9), not re-checked here.)
  - **Trade frequency** — closed-trade count vs the Phase 3.2.4 prep
    target of 60 (30/yr × 2yr); < 20 risks statistical insignificance,
    > 200 suggests the backtest is too easy to satisfy.
  - **PnL distribution** — summary stats over realized R, so a human
    can eyeball whether it looks like a real distribution or a
    suspicious single-spike.
  - **Spread (slippage) sanity** — the bid/ask spread of the traded
    contracts as a fraction of mid, against the ``slippage_pct``
    assumption, so we know whether the backtest's execution-cost model
    is optimistic.

No IO and no rendering — callers feed trades/spreads and render the
findings into ``docs/phase-3.5.7-audit.md``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.backtest.pnl_provider import RealizedTrade


# ---------------------------------------------------------------------------
# Look-ahead leakage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeakageReport:
    """Result of the trade-level look-ahead check."""

    closed_trades: int
    violations: tuple[str, ...]
    """event_ids of closed trades whose exit is missing, not strictly after
    entry, or priced off a quote outside the exit's own session — each is a
    time-travel bug."""
    unverifiable: tuple[str, ...] = ()
    """event_ids of closed trades carrying no ``exit_quote_ts``, so the quote
    behind the exit cannot be checked here.

    Not violations, and deliberately not silence: a provider that drops its
    quote's timestamp leaves this check blind, and the count says how blind.
    """

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def fully_verified(self) -> bool:
        """True only when every closed trade could actually be checked."""
        return not self.violations and not self.unverifiable


def check_lookahead(trades: Sequence[RealizedTrade]) -> LeakageReport:
    """Every closed trade must exit after it enters, priced off its own session.

    Two things are checked, and the second is why this function was rewritten on
    2026-09-20.

    The first — ``exit_ts > entry_ts`` — catches a negative holding period. It does
    **not** catch an exit priced off a quote from another day, which is exactly the
    leak the 2026-09-19 audit found in ``parquet_exit_quote``: the trade's own
    timestamps were impeccable while the bid that priced it had been recorded 18 days
    before the position opened. That produced ``realized_r=+0.1467`` out of nothing,
    and this function passed it — the provider's own docstring said so, calling the
    defect "invisible to sanity_audit.check_lookahead, which compares the trade's own
    timestamps and never the quote's".

    So when a trade carries ``exit_quote_ts``, the quote must sit inside the
    position's life and on the exit's own calendar day. The same-day rule is the one
    the guarded sibling in ``simple_pnl`` has enforced since 3.5.0.1; this makes the
    audit able to see it rather than trusting each provider to have got it right.

    A trade carrying no ``exit_quote_ts`` is reported as **unverifiable** rather than
    passed. Counting it as clean would restore exactly the blindness this change
    removes, and a check that cannot say what it could not see is a formality.
    """
    closed = [t for t in trades if t.realized_r is not None]
    violations: list[str] = []
    unverifiable: list[str] = []
    for trade in closed:
        if trade.exit_ts is None or trade.exit_ts <= trade.entry_ts:
            violations.append(trade.event_id)
            continue
        quote_ts = trade.exit_quote_ts
        if quote_ts is None:
            unverifiable.append(trade.event_id)
        elif not (trade.entry_ts <= quote_ts <= trade.exit_ts):
            # A quote from before the position opened, or from after it closed.
            violations.append(trade.event_id)
        elif quote_ts.date() != trade.exit_ts.date():
            # Inside the position's life but from an earlier session: the option did
            # not quote on the exit day and the bid is stale, not a price.
            violations.append(trade.event_id)
    return LeakageReport(
        closed_trades=len(closed),
        violations=tuple(violations),
        unverifiable=tuple(unverifiable),
    )


# ---------------------------------------------------------------------------
# Trade frequency
# ---------------------------------------------------------------------------


_FREQ_TOO_FEW = 20
_FREQ_TOO_MANY = 200
_FREQ_TARGET = 60


@dataclass(frozen=True)
class FrequencyReport:
    """Closed-trade-count assessment against the pinned target band."""

    closed_trades: int
    target: int
    too_few: bool
    too_many: bool

    @property
    def verdict(self) -> str:
        if self.too_few:
            return "too_few — statistical insignificance risk (< 20)"
        if self.too_many:
            return "too_many — backtest may be too easy to satisfy (> 200)"
        return "ok"


def assess_trade_frequency(closed_trades: int) -> FrequencyReport:
    """Classify a closed-trade count against the 60-trade prep target."""
    return FrequencyReport(
        closed_trades=closed_trades,
        target=_FREQ_TARGET,
        too_few=closed_trades < _FREQ_TOO_FEW,
        too_many=closed_trades > _FREQ_TOO_MANY,
    )


# ---------------------------------------------------------------------------
# PnL distribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionReport:
    """Summary stats over realized R for the closed trades."""

    n: int
    mean_r: float | None
    median_r: float | None
    stdev_r: float | None
    min_r: float | None
    max_r: float | None
    win_rate: float | None


def pnl_distribution(trades: Sequence[RealizedTrade]) -> DistributionReport:
    """Summarise the realized-R distribution of the closed trades."""
    rs = [t.realized_r for t in trades if t.realized_r is not None]
    if not rs:
        return DistributionReport(0, None, None, None, None, None, None)
    wins = sum(1 for r in rs if r > 0)
    return DistributionReport(
        n=len(rs),
        mean_r=statistics.fmean(rs),
        median_r=statistics.median(rs),
        stdev_r=statistics.stdev(rs) if len(rs) > 1 else 0.0,
        min_r=min(rs),
        max_r=max(rs),
        win_rate=wins / len(rs),
    )


# ---------------------------------------------------------------------------
# Spread (slippage) sanity
# ---------------------------------------------------------------------------


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (q in [0, 1])."""
    if not sorted_vals:
        msg = "percentile of empty sequence"
        raise ValueError(msg)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


@dataclass(frozen=True)
class SpreadReport:
    """Bid/ask spread (as a fraction of mid) of the traded contracts vs
    the execution-cost assumption."""

    n: int
    median_pct: float | None
    mean_pct: float | None
    p90_pct: float | None
    p99_pct: float | None
    frac_over_assumption: float | None
    slippage_assumption_pct: float

    @property
    def optimistic(self) -> bool:
        """True when the median spread alone exceeds the slippage haircut —
        a flag that the execution-cost model may be too kind."""
        return (
            self.median_pct is not None
            and self.median_pct > self.slippage_assumption_pct
        )


def spread_sanity(
    spread_pcts: Sequence[float],
    *,
    slippage_assumption_pct: float,
) -> SpreadReport:
    """Summarise spread-as-fraction-of-mid against the slippage assumption.

    ``spread_pcts`` are ``(ask - bid) / mid`` values (e.g. 0.012 for a
    1.2% spread). ``slippage_assumption_pct`` is the profile's
    ``backtest.slippage_pct`` (e.g. 0.02).
    """
    vals = sorted(float(s) for s in spread_pcts)
    if not vals:
        return SpreadReport(
            n=0, median_pct=None, mean_pct=None, p90_pct=None,
            p99_pct=None, frac_over_assumption=None,
            slippage_assumption_pct=slippage_assumption_pct,
        )
    over = sum(1 for s in vals if s > slippage_assumption_pct)
    return SpreadReport(
        n=len(vals),
        median_pct=_percentile(vals, 0.5),
        mean_pct=statistics.fmean(vals),
        p90_pct=_percentile(vals, 0.9),
        p99_pct=_percentile(vals, 0.99),
        frac_over_assumption=over / len(vals),
        slippage_assumption_pct=slippage_assumption_pct,
    )
