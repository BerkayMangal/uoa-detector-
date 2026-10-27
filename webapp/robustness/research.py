"""Offline, bounded diagnostics; never searches or promotes a replacement strategy."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from webapp.alpha_lab.db import digest
from webapp.calibration.model import load_csv
from webapp.robustness.settings import settings
from webapp.tournament import report
from webapp.tournament.engine import Simulation, Trade, simulate
from webapp.tournament.model import BoolArray, Config, FloatArray, Prepared, mask, prepare
from webapp.tournament.settings import settings as tournament_settings


def delayed_inputs(
    p: Prepared, c: Config, selected: BoolArray, start: date, delay: int
) -> tuple[Prepared, BoolArray]:
    """Shift instructions, ranks and size inputs; actual execution prices stay put."""
    if delay < 0:
        raise ValueError("entry delay must be nonnegative")
    eligible = selected.copy()
    eligible[[i for i, d in enumerate(p.days) if d < start]] = False
    if not delay:
        return p, eligible
    shifted = np.zeros_like(eligible)
    if delay < len(p.days):
        shifted[delay:] = eligible[:-delay]

    def move(a: FloatArray) -> FloatArray:
        out = np.full_like(a, np.nan)
        if delay < len(p.days):
            out[delay:] = a[:-delay]
        return out

    key = c.family, c.lookback
    return replace(p, atr=move(p.atr), scores=p.scores | {key: move(p.scores[key])}), shifted


def accounting(trades: tuple[Trade, ...]) -> dict[str, Any]:
    """JSON boundary: removing trades is ledger arithmetic, not portfolio replay."""
    closed = [t for t in trades if t.pnl is not None]
    if any(not math.isfinite(t.pnl or 0) for t in closed):
        raise ValueError("nonfinite closed P&L")
    net = sum(t.pnl or 0 for t in closed) if closed else None
    positive = sorted((t.pnl for t in closed if t.pnl is not None and t.pnl > 0), reverse=True)
    gains = sum(positive)
    groups: dict[str, list[dict[str, Any]]] = {}
    for field in ("ticker", "signal_month"):
        keys = sorted({t.ticker if field == "ticker" else str(t.signal)[:7] for t in closed})
        rows = []
        for key in keys:
            ts = [
                t for t in closed if (t.ticker if field == "ticker" else str(t.signal)[:7]) == key
            ]
            rows.append(
                {
                    "key": key,
                    "closed": len(ts),
                    "net_usd": sum(t.pnl or 0 for t in ts),
                    "excess_usd": sum(t.excess for t in ts if t.excess is not None)
                    if all(t.excess is not None for t in ts)
                    else None,
                }
            )
        groups[field] = rows
    return {
        "closed": len(closed),
        "unresolved": len(trades) - len(closed),
        "net_usd": net,
        "gross_gains_usd": gains if closed else None,
        "top_win_share_pct": positive[0] / gains * 100 if gains else None,
        "ticker_count": len(groups["ticker"]),
        "without_best": {
            str(n): {
                "net_usd": net - sum(positive[:n]) if net is not None else None,
                "removed": min(n, len(positive)),
            }
            for n in settings().remove_best_trades
        },
        "groups": groups,
    }


def serialized(sim: Simulation, p: Prepared, delay: int) -> dict[str, Any]:
    indices = {d: i for i, d in enumerate(p.days)}
    return {
        "metrics": asdict(sim.metrics),
        "ledger": [
            asdict(t) | {"original_signal": p.days[indices[t.signal] - delay]} for t in sim.trades
        ],
        "equity": sim.equity,
        "daily_excess": sim.daily_excess,
    }


def omit(p: Prepared, original: BoolArray, field: str, key: str) -> BoolArray:
    selected = original.copy()
    if field == "ticker":
        selected[:, p.tickers.index(key)] = False
    elif field == "signal_month":
        selected[[i for i, d in enumerate(p.days) if str(d)[:7] == key]] = False
    else:
        raise ValueError("unknown omission type")
    return selected


def assert_baseline(sim: Simulation, expected: dict[str, Any]) -> None:
    # Normalize dates/tuples through the same canonical serializer, without rounding.
    actual = {
        "metrics": asdict(sim.metrics),
        "ledger": [asdict(t) for t in sim.trades],
        "equity": sim.equity,
        "daily_excess": sim.daily_excess,
    }
    if digest(actual) != digest(expected):
        raise ValueError("baseline differs from frozen tournament")


def run(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return summary and full deterministic records to the offline writer only."""
    from webapp.robustness.report import code_hash

    frozen, s, ts = report.load(), settings(), tournament_settings()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    source_key = "data/study_f/bars.csv"
    if frozen["source_hashes"].get(source_key) != source_hash:
        raise ValueError("source differs from frozen tournament")
    p = prepare(load_csv(source))
    if list(p.tickers) != frozen["tickers"]:
        raise ValueError("source universe differs from frozen tournament")
    names = tuple(t for t in p.tickers if t != "SPY")
    months = sorted({str(d)[:7] for d in p.days if ts.test_start <= d <= ts.test_end})
    records: list[dict[str, Any]] = []  # Explicit artifact JSON boundary.
    summaries = []
    for winner in frozen["winners"]:
        c = Config(**winner["config"])
        original = mask(p, c)
        baseline = simulate(
            p, c, original, ts.test_start, ts.test_end, ts.slippage_bps, ledger=True
        )
        assert_baseline(baseline, winner["test"])
        costs: list[dict[str, Any]] = []
        omissions: dict[str, list[dict[str, Any]]] = {"ticker": [], "signal_month": []}

        def record(
            mode: str,
            detail: dict[str, Any],
            sim: Simulation,
            delay: int = 0,
            config_id: str = c.id,
        ) -> dict[str, Any]:
            identity = {"config_id": config_id, "mode": mode} | detail
            row = identity | {"key": digest(identity)} | serialized(sim, p, delay)
            records.append(row)
            return identity | {"metrics": asdict(sim.metrics)}

        for delay in s.entry_delays:
            delayed, selected = delayed_inputs(p, c, original, ts.test_start, delay)
            for bps in s.slippage_bps:
                sim = simulate(delayed, c, selected, ts.test_start, ts.test_end, bps, ledger=True)
                costs.append(record("delay_cost", {"delay": delay, "bps": bps}, sim, delay))
        for field, keys in (("ticker", names), ("signal_month", months)):
            for key in keys:
                selected = omit(p, original, field, key)
                sim = simulate(
                    p, c, selected, ts.test_start, ts.test_end, ts.slippage_bps, ledger=True
                )
                omissions[field].append(record("exclude_" + field, {"excluded": key}, sim))
        summaries.append(
            {
                "config_id": c.id,
                "family": c.family,
                "paper_enabled": winner["paper_enabled"],
                "accounting": accounting(baseline.trades),
                "delay_cost": costs,
                "omissions": omissions,
            }
        )
        print(f"audited {c.family}: {len(records)} cumulative replays", flush=True)
    return {
        "version": s.version,
        "profile_hash": s.profile_hash,
        "code_hash": code_hash(),
        "base_report_hash": frozen["report_hash"],
        "source_hashes": {source_key: source_hash},
        "test_start": ts.test_start,
        "test_end": ts.test_end,
        "portfolio_evaluations": len(records),
        "selected_configurations": len(summaries),
        "baseline_reconciliations": len(summaries),
        "delay_values": s.entry_delays,
        "slippage_bps": s.slippage_bps,
        "excluded_names": len(names),
        "excluded_signal_months": months,
        "families": summaries,
        "evidence": "REUSED_HISTORY_DIAGNOSTICS_ONLY_NO_NEW_SELECTION",
    }, records
