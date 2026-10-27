"""Cash-constrained, next-open stock simulations with matched reference marks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

from webapp.calibration.model import horizon_days
from webapp.tournament.model import BoolArray, Config, Prepared
from webapp.tournament.settings import settings


@dataclass(frozen=True)
class Trade:
    ticker: str
    signal: date
    entry: date
    exit: date
    quantity: int
    debit: float
    credit: float | None
    pnl: float | None
    excess: float | None


@dataclass(frozen=True)
class Metrics:
    closed: int
    unresolved: int
    missed_entries: int
    missing_marks: int
    matched: int
    net_usd: float
    excess_usd: float | None
    max_drawdown_pct: float
    return_pct: float
    wins: int
    losses: int


@dataclass(frozen=True)
class Simulation:
    metrics: Metrics
    daily_excess: tuple[float, ...]
    trades: tuple[Trade, ...]
    equity: tuple[float, ...]


@dataclass(frozen=True)
class _Position:
    signal: int
    entry: int
    end: int
    quantity: int
    debit: float
    spy_entry: float | None


def positive(n: float) -> bool:
    return math.isfinite(n) and n > 0


def simulate(
    p: Prepared,
    c: Config,
    selected: BoolArray,
    start: date,
    end: date,
    bps: float,
    *,
    ledger: bool = False,
) -> Simulation:
    s = settings()
    indices = {d: i for i, d in enumerate(p.days)}
    scan = [i for i, d in enumerate(p.days) if start <= d <= end]
    slip = bps / 10000
    cash = peak = s.capital_usd
    max_dd = 0.0
    active: dict[int, _Position] = {}
    marks: dict[int, float] = {}
    previous_excess = closed_net = closed_excess = 0.0
    closed = matched = missing = missed = wins = losses = 0
    daily = []
    equity_path = []
    trades = []
    spy = p.tickers.index("SPY")
    dates = {i: horizon_days(p.days[i], c.horizon) for i in scan}
    for i in scan:
        signal = i - 1
        if signal >= 0 and p.days[signal] >= start:
            expected = dates.get(signal)
            if expected is not None and expected[0] == p.days[i] and expected[1] <= end:
                due = indices.get(expected[1], -1)
                if due >= 0:
                    candidates = np.flatnonzero(selected[signal])
                    rank = p.scores[c.family, c.lookback][signal, candidates]
                    for j0 in candidates[np.argsort(-rank, kind="stable")]:
                        j = int(j0)
                        if j in active or len(active) >= c.slots:
                            continue
                        px = float(p.open[i, j])
                        if not positive(px):
                            missed += 1
                            continue
                        entry = px * (1 + slip)
                        qty = int(min(cash, s.position_usd) / entry)
                        if c.sizing == "atr_budget":
                            atr = float(p.atr[signal, j])
                            qty = (
                                min(qty, int(s.risk_usd / (s.risk_atr_multiple * atr)))
                                if positive(atr)
                                else 0
                            )
                        if qty <= 0:
                            continue
                        debit = entry * qty
                        cash -= debit
                        spy_entry = float(p.open[i, spy])
                        active[j] = _Position(
                            signal, i, due, qty, debit, spy_entry if positive(spy_entry) else None
                        )
                        marks[j] = px
        active_excess = 0.0
        for j, t in list(active.items()):
            px = float(p.close[i, j])
            spy_close = float(p.close[i, spy])
            if positive(px):
                marks[j] = px
            else:
                missing += 1
            reference = (
                t.debit * spy_close * (1 - slip) / (t.spy_entry * (1 + slip))
                if t.spy_entry is not None and positive(spy_close)
                else None
            )
            if reference is None:
                missing += 1
            credit = marks[j] * t.quantity * (1 - slip)
            excess = credit - reference if reference is not None else None
            if i == t.end and positive(px):
                cash += credit
                closed_net += credit - t.debit
                closed_excess += excess if excess is not None else 0
                closed += 1
                matched += int(excess is not None)
                wins += int(credit > t.debit)
                losses += int(credit < t.debit)
                if ledger:
                    trades.append(
                        Trade(
                            p.tickers[j],
                            p.days[t.signal],
                            p.days[t.entry],
                            p.days[t.end],
                            t.quantity,
                            t.debit,
                            credit,
                            credit - t.debit,
                            excess,
                        )
                    )
                del active[j]
            else:
                active_excess += excess if excess is not None else 0
        equity = cash + sum(t.quantity * marks[j] * (1 - slip) for j, t in active.items())
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        current_excess = closed_excess + active_excess
        daily.append((current_excess - previous_excess) / s.capital_usd)
        previous_excess = current_excess
        if ledger:
            equity_path.append(equity)
    if ledger:
        trades.extend(
            Trade(
                p.tickers[j],
                p.days[t.signal],
                p.days[t.entry],
                p.days[t.end],
                t.quantity,
                t.debit,
                None,
                None,
                None,
            )
            for j, t in active.items()
        )
    final_equity = cash + sum(t.quantity * marks[j] * (1 - slip) for j, t in active.items())
    metrics = Metrics(
        closed,
        len(active),
        missed,
        missing,
        matched,
        closed_net,
        closed_excess if matched == closed and missing == 0 else None,
        max_dd,
        (final_equity / s.capital_usd - 1) * 100,
        wins,
        losses,
    )
    return Simulation(metrics, tuple(daily), tuple(trades), tuple(equity_path))
