"""Four frozen daily proxies, causal decisions and exact-session cash accounting."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise

from uoa_detector.live_alpha.calendar import is_session_day, next_session_after
from webapp.alpha_lab.research import following_sessions
from webapp.calibration.settings import calendar
from webapp.call_research.data import Dataset
from webapp.call_research.settings import Settings, Window, settings


@dataclass(frozen=True)
class Signal:
    ticker: str
    arm: str
    signal_day: date
    decision_day: date
    entry_day: date
    score: float


@dataclass(frozen=True)
class Trade:
    ticker: str
    signal_day: date
    decision_day: date
    entry_day: date
    target_day: date
    quantity: int
    entry_price: float
    debit: float
    exit_day: date | None = None
    exit_price: float | None = None
    credit: float | None = None
    pnl: float | None = None
    spy_excess: float | None = None
    qqq_excess: float | None = None


@dataclass(frozen=True)
class Equity:
    day: date
    cash: float
    liquidation: float
    spy_daily_excess_pp: float | None
    qqq_daily_excess_pp: float | None


@dataclass(frozen=True)
class Replay:
    arm: str
    window: str
    horizon: int
    cost_bps: float
    signals: int
    boundary_excluded: int
    capacity_skips: int
    overlap_skips: int
    missing_entries: int
    unresolved: int
    closed: int
    wins: int
    net_usd: float
    return_pct: float | None
    max_drawdown_pct: float | None
    spy_excess_usd: float | None
    qqq_excess_usd: float | None
    trades: tuple[Trade, ...]
    equity: tuple[Equity, ...]


def signals(data: Dataset, cfg: Settings | None = None) -> tuple[Signal, ...]:
    s, cal = cfg or settings(), calendar()
    panel = {(b.ticker, b.day): b for b in data.bars}
    result = []

    def close(name: str, day: date) -> float | None:
        b = panel.get((name, day))
        return b.close if b and b.valid else None

    for i, day in enumerate(data.days):
        if not i or next_session_after(data.days[i - 1], cal) != day:
            continue
        previous = data.days[i - 1]
        spy0, spy1 = close(s.benchmarks[0], previous), close(s.benchmarks[0], day)
        if spy0 is None or spy1 is None or spy1 >= spy0:
            continue
        market = spy1 / spy0 - 1
        entry = next_session_after(day, cal)
        if entry is None:
            continue
        for ticker in data.stocks:
            own0, own1 = close(ticker, previous), close(ticker, day)
            if own0 is None or own1 is None:
                continue
            own = own1 / own0 - 1
            excess = own - market
            if own <= 0 or excess < s.relative_min:
                continue
            result.append(Signal(ticker, "baseline", day, day, entry, excess))
            if i >= s.persistence_sessions:
                prefix = data.days[i - s.persistence_sessions : i + 1]
                prices = [close(ticker, d) for d in prefix]
                spy = [close(s.benchmarks[0], d) for d in prefix]
                q0, q1 = close(s.benchmarks[1], prefix[0]), close(s.benchmarks[1], day)
                valid = all(next_session_after(a, cal) == b for a, b in pairwise(prefix)) and all(
                    v is not None for v in prices + spy
                )
                if valid and q0 is not None and q1 is not None:
                    p = [v for v in prices if v is not None]
                    m = [v for v in spy if v is not None]
                    if (
                        all(p[j] / p[j - 1] > m[j] / m[j - 1] for j in range(1, len(p)))
                        and p[-1] / p[0] > q1 / q0
                    ):
                        result.append(Signal(ticker, "persistence", day, day, entry, excess))
            delayed = following_sessions(day, s.retest_delay_sessions, cal)
            if not delayed or delayed[-1] > data.days[-1]:
                continue
            decision = delayed[-1]
            second_entry = next_session_after(decision, cal)
            if second_entry is None:
                continue
            result.append(Signal(ticker, "delay_control", day, decision, second_entry, excess))
            bar = panel.get((ticker, decision))
            if bar and bar.valid and bar.low <= own1 and bar.close >= own1:
                result.append(Signal(ticker, "retest", day, decision, second_entry, excess))
    return tuple(result)


def simulate(
    data: Dataset,
    candidates: tuple[Signal, ...],
    arm: str,
    window: Window,
    horizon: int,
    cost_bps: float,
    cfg: Settings | None = None,
) -> Replay:
    s, cal = cfg or settings(), calendar()
    panel = {(b.ticker, b.day): b for b in data.bars}
    waiting: dict[date, list[tuple[Signal, date]]] = defaultdict(list)
    boundary = capacity = overlap = missing = count = 0
    for f in candidates:
        if f.arm != arm or not window.start <= f.signal_day <= window.end:
            continue
        count += 1
        dates = (f.entry_day, *following_sessions(f.entry_day, horizon - 1, cal))
        if len(dates) != horizon or dates[-1] > window.end:
            boundary += 1
            continue
        waiting[f.entry_day].append((f, dates[-1]))
    active: dict[str, Trade] = {}
    marks: dict[str, float] = {}
    ledger: list[Trade] = []
    path: list[Equity] = []
    cash = peak = s.capital_usd
    drawdown = 0.0
    previous_excess: list[float | None] = [0.0, 0.0]
    slip = cost_bps / 10000
    day = window.start
    any_price_gap = False
    while day <= window.end:
        if is_session_day(day, cal) is not True:
            day += timedelta(days=1)
            continue
        for f, expiry in sorted(
            waiting.get(day, ()), key=lambda item: (-item[0].score, item[0].ticker)
        ):
            if f.ticker in active:
                overlap += 1
                continue
            if len(active) >= s.max_positions:
                capacity += 1
                continue
            b = panel.get((f.ticker, day))
            if not b or not b.valid:
                missing += 1
                continue
            price = b.open * (1 + slip)
            qty = math.floor(min(s.position_usd, cash) / price)
            if qty <= 0:
                capacity += 1
                continue
            debit = qty * price
            trade = Trade(f.ticker, f.signal_day, f.decision_day, day, expiry, qty, b.open, debit)
            active[f.ticker] = trade
            marks[f.ticker] = b.open
            cash -= debit
        for ticker, t in list(active.items()):
            b = panel.get((ticker, day))
            if b and b.valid:
                marks[ticker] = b.close
            else:
                any_price_gap = True
            if day != t.target_day or not b or not b.valid:
                continue
            credit = t.quantity * b.close * (1 - slip)
            pnl = credit - t.debit
            excess_values: list[float | None] = []
            for benchmark in s.benchmarks:
                first, last = panel.get((benchmark, t.entry_day)), panel.get((benchmark, day))
                excess_values.append(
                    pnl - t.debit * (last.close * (1 - slip) / (first.open * (1 + slip)) - 1)
                    if first and last and first.valid and last.valid
                    else None
                )
            ledger.append(
                replace(
                    t,
                    exit_day=day,
                    exit_price=b.close,
                    credit=credit,
                    pnl=pnl,
                    spy_excess=excess_values[0],
                    qqq_excess=excess_values[1],
                )
            )
            cash += credit
            del active[ticker]
            marks.pop(ticker, None)
        liquidation = cash + sum(
            t.quantity * marks[name] * (1 - slip) for name, t in active.items()
        )
        peak = max(peak, liquidation)
        drawdown = max(drawdown, (peak - liquidation) / peak * 100)
        current: list[float | None] = []
        for index, benchmark in enumerate(s.benchmarks):
            known = [t.spy_excess if index == 0 else t.qqq_excess for t in ledger]
            excess = sum(v for v in known if v is not None)
            valid = all(v is not None for v in known)
            for name, t in active.items():
                first, last = panel.get((benchmark, t.entry_day)), panel.get((benchmark, day))
                own = panel.get((name, day))
                if (
                    not first
                    or not last
                    or not own
                    or not first.valid
                    or not last.valid
                    or not own.valid
                ):
                    valid = False
                    continue
                excess += (
                    t.quantity * own.close * (1 - slip)
                    - t.debit
                    - t.debit * (last.close * (1 - slip) / (first.open * (1 + slip)) - 1)
                )
            current.append(excess if valid else None)
        changes = tuple(
            (value - prev) / s.capital_usd * 100 if value is not None and prev is not None else None
            for value, prev in zip(current, previous_excess, strict=True)
        )
        path.append(Equity(day, cash, liquidation, changes[0], changes[1]))
        previous_excess = current
        day += timedelta(days=1)
    all_trades = tuple(ledger) + tuple(active.values())
    net = sum(t.pnl or 0 for t in ledger)
    refs = []
    for index in range(len(s.benchmarks)):
        values = [t.spy_excess if index == 0 else t.qqq_excess for t in ledger]
        refs.append(
            sum(v for v in values if v is not None)
            if not active and all(v is not None for v in values)
            else None
        )
    return Replay(
        arm,
        window.name,
        horizon,
        cost_bps,
        count,
        boundary,
        capacity,
        overlap,
        missing,
        len(active),
        len(ledger),
        sum(t.pnl is not None and t.pnl > 0 for t in ledger),
        net,
        (cash / s.capital_usd - 1) * 100 if not active else None,
        drawdown if not any_price_gap else None,
        refs[0],
        refs[1],
        all_trades,
        tuple(path),
    )
