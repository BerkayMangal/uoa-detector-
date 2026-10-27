"""Bounded search: cash at next open, calendar exits, validation-only selection."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from uoa_detector.live_alpha.calendar import is_session_day
from webapp.alpha_lab.db import digest
from webapp.calibration.model import Bar, Config, Feature, features, grid, horizon_days, matches
from webapp.calibration.settings import calendar, settings


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
    spy_excess: float | None


@dataclass(frozen=True)
class Metrics:
    closed: int
    unresolved: int
    missed_entries: int
    net_usd: float
    equity_return_pct: float
    max_drawdown_pct: float
    spy_excess_pp: float | None
    matched: int


def simulate(c: Config, fs: tuple[Feature, ...], bars: tuple[Bar, ...], start: date, end: date,
             horizon: int, slippage_bps: float) -> tuple[Metrics, tuple[Trade, ...]]:
    s, cal = settings(), calendar()
    panel = {(b.ticker, b.day): b for b in bars}
    signals: dict[date, list[tuple[Feature, date]]] = {}
    for f in fs:
        if not start <= f.day <= end or not matches(f, c):
            continue
        dates = horizon_days(f.day, horizon)
        if dates is not None and dates[1] <= end:
            signals.setdefault(dates[0], []).append((f, dates[1]))
    cash, peak, decline = s.capital_usd, s.capital_usd, 0.0
    active: dict[str, Trade] = {}
    marks: dict[str, float] = {}
    ledger = []
    missed = 0
    slip = slippage_bps / 10000
    day = start
    while day <= end:
        if is_session_day(day, cal) is not True:
            day += timedelta(days=1)
            continue
        for f, expiry in sorted(signals.get(day, []), key=lambda pair: (-pair[0].lag, pair[0].ticker)):
            if f.ticker in active or len(active) >= s.max_positions:
                continue
            b = panel.get((f.ticker, day))
            if b is None or not b.valid:
                missed += 1
                continue
            price = b.open * (1 + slip)
            qty = int(min(cash, s.position_usd) / price)
            if qty <= 0:
                continue
            debit = qty * price
            cash -= debit
            active[f.ticker] = Trade(f.ticker, f.day, day, expiry, qty, debit, None, None, None)
            marks[f.ticker] = b.open
        for ticker, t in list(active.items()):
            b = panel.get((ticker, day))
            if b is not None and b.valid:
                marks[ticker] = b.close
            if t.exit != day or b is None or not b.valid:
                continue
            credit = b.close * (1-slip) * t.quantity
            spy_entry, spy_exit = panel.get(("SPY", t.entry)), panel.get(("SPY", t.exit))
            excess = None
            if spy_entry is not None and spy_exit is not None and spy_entry.valid and spy_exit.valid:
                excess = ((credit / t.debit) - (spy_exit.close * (1-slip) / (spy_entry.open * (1+slip)))) * 100
            ledger.append(Trade(t.ticker, t.signal, t.entry, t.exit, t.quantity, t.debit, credit, credit-t.debit, excess))
            cash += credit
            del active[ticker]
        equity = cash + sum(t.quantity * marks[ticker] * (1-slip) for ticker, t in active.items())
        peak = max(peak, equity)
        decline = max(decline, (peak-equity) / peak * 100)
        day += timedelta(days=1)
    unresolved = tuple(active.values())
    returns = [t.spy_excess for t in ledger if t.spy_excess is not None]
    return Metrics(len(ledger), len(unresolved), missed, sum(t.pnl or 0 for t in ledger),
                   (cash + sum(t.quantity * marks[ticker] * (1-slip) for ticker, t in active.items()) - s.capital_usd) / s.capital_usd * 100,
                   decline, sum(returns)/len(returns) if returns else None, len(returns)), tuple(ledger) + unresolved


def eligible(m: Metrics, drawdown: bool = True) -> bool:
    s = settings()
    return m.closed >= s.min_closed and m.unresolved == 0 and m.matched == m.closed and m.net_usd > 0 and m.spy_excess_pp is not None and m.spy_excess_pp > 0 and (not drawdown or m.max_drawdown_pct <= s.max_drawdown_pct)


def choose(rows: tuple[tuple[Config, Metrics, Metrics], ...]) -> Config | None:
    candidates = [(c, train, val) for c, train, val in rows if eligible(train, False) and eligible(val)]
    candidates.sort(key=lambda r: (-(r[2].spy_excess_pp or 0), r[2].max_drawdown_pct, r[0].id))
    return candidates[0][0] if candidates else None


def run(bars: tuple[Bar, ...], source: Path) -> dict[str, Any]:
    # Any here is the serialized report adapter, never a live decision input.
    s = settings()
    all_features = {n: features(bars, n) for n in s.lookbacks}
    scored = []
    for c in grid():
        train, _ = simulate(c, all_features[c.lookback], bars, s.train_start, s.train_end, s.primary_sessions, s.slippage_bps)
        val, _ = simulate(c, all_features[c.lookback], bars, s.validation_start, s.validation_end, s.primary_sessions, s.slippage_bps)
        scored.append((c, train, val))
    selected = choose(tuple(scored))
    selected_report = None
    test_passed = False
    if selected is not None:
        test, trades = simulate(selected, all_features[selected.lookback], bars, s.test_start, s.test_end, s.primary_sessions, s.slippage_bps)
        test_passed = eligible(test)
        diagnostics = {str(h): asdict(simulate(selected, all_features[selected.lookback], bars, s.test_start, s.test_end, h, s.slippage_bps)[0]) for h in s.secondary_sessions}
        sensitivity = {str(bp): asdict(simulate(selected, all_features[selected.lookback], bars, s.test_start, s.test_end, s.primary_sessions, bp)[0]) for bp in s.sensitivity_bps}
        selected_report = {"id": selected.id, "config": asdict(selected), "test": asdict(test), "secondary": diagnostics, "sensitivity": sensitivity, "ledger": [asdict(t) for t in trades]}
    metrics = [{"id": c.id, "config": asdict(c), "train": asdict(train), "validation": asdict(val)} for c, train, val in scored]
    body = {"version": s.version, "evidence": "EXPLORATORY_ONLY_REUSED_STUDY_F", "data_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "profile_sha256": s.profile_hash, "code_sha256": code_hash(), "calendar": calendar().model_dump(mode="json"),
            "rows": len(bars), "tickers": sorted({b.ticker for b in bars}), "first": str(min(b.day for b in bars)), "last": str(max(b.day for b in bars)),
            "configuration_trials": len(scored), "portfolio_evaluations": 2*len(scored)+(1+len(s.secondary_sessions)+len(s.sensitivity_bps) if selected else 0),
            "train_eligible": sum(eligible(t, False) for _, t, _ in scored), "validation_eligible": sum(eligible(t, False) and eligible(v) for _, t, v in scored),
            "verdict": "EXPERIMENTAL_FORWARD_ONLY" if test_passed else "REJECTED" if selected else "NO_CANDIDATE",
            "prospective_start": str(s.prospective_start), "selected": selected_report, "grid": metrics}
    return body | {"report_sha256": digest(body)}


def code_hash() -> str:
    return hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes() for name in ("model.py", "search.py", "settings.py", "monitor.py", "storage.py", "report.py"))).hexdigest()
