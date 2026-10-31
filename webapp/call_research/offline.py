"""Explicit offline replay, never invoked by HTTP. All historical data is reused."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from webapp.call_research import report
from webapp.call_research.backtest import Replay, signals, simulate
from webapp.call_research.data import load
from webapp.call_research.settings import profile_hash, settings


def _encode(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        default=_encode,
        separators=(",", ":"),
    )


def interval(values: tuple[float | None, ...], seed: int) -> tuple[float, float] | None:
    s = settings()
    if len(values) < s.bootstrap_block_sessions or any(v is None for v in values):
        return None
    data = np.asarray([v for v in values if v is not None], dtype=float)
    blocks = (len(data) + s.bootstrap_block_sessions - 1) // s.bootstrap_block_sessions
    starts = np.random.default_rng(seed).integers(0, len(data), size=(s.bootstrap_reps, blocks, 1))
    indices = (starts + np.arange(s.bootstrap_block_sessions)) % len(data)
    means = data[indices.reshape(s.bootstrap_reps, -1)[:, : len(data)]].mean(axis=1)
    bounds = np.quantile(means, s.bootstrap_quantiles)
    return float(bounds[0]), float(bounds[1])


def summary(replay: Replay, index: int) -> dict[str, Any]:
    # Immutable artifact serialization adapter; no untyped records enter the simulator.
    body = asdict(replay)
    body.pop("trades")
    body.pop("equity")
    wins = sorted((t.pnl for t in replay.trades if t.pnl is not None and t.pnl > 0), reverse=True)
    body["remove_best_wins"] = {
        str(n): {"removed": min(n, len(wins)), "remaining_net_usd": replay.net_usd - sum(wins[:n])}
        for n in settings().remove_best_wins
    }
    body["largest_win_share"] = wins[0] / sum(wins) if wins else None
    months: dict[str, float] = {}
    for t in replay.trades:
        if t.pnl is not None:
            month = t.signal_day.strftime("%Y-%m")
            months[month] = months.get(month, 0) + t.pnl
    body["signal_month_pnl"] = months
    if (
        replay.horizon == settings().primary_horizon
        and replay.cost_bps == settings().slippage_bps[0]
    ):
        body["spy_daily_excess_interval_pp"] = interval(
            tuple(e.spy_daily_excess_pp for e in replay.equity), settings().bootstrap_seed + index
        )
        body["qqq_daily_excess_interval_pp"] = interval(
            tuple(e.qqq_daily_excess_pp for e in replay.equity), settings().bootstrap_seed + index
        )
    return body


def run(directory: Path) -> dict[str, Any]:
    s = settings()
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("refuse to overwrite research evidence")
    data = load()
    candidates = signals(data)
    lines: list[str] = []
    scores: list[dict[str, Any]] = []
    for window in s.windows:
        for arm in s.arms:
            for horizon in s.horizons:
                for cost in s.slippage_bps:
                    replay = simulate(data, candidates, arm, window, horizon, cost)
                    scores.append(summary(replay, len(scores)))
                    lines.append(_json(asdict(replay)))
    serialized = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
    body = {
        "version": s.version,
        "classification": "EXPLORATORY_REUSED_DATA",
        "live_promotion": False,
        "source_hash": data.source_hash,
        "profile_hash": profile_hash(),
        "code_hash": report.code_hash(),
        "rows": len(data.bars),
        "sessions": len(data.days),
        "stock_universe": data.stocks,
        "missing_historical_context": data.missing_context,
        "first_day": min(data.days).isoformat(),
        "last_day": max(data.days).isoformat(),
        "replays": len(scores),
        "optimization_trials": 0,
        "interval_scope": "descriptive circular session-block bootstrap; recycled data; no fresh significance test",
        "limits": [
            "daily price proxy, not the live UOA strategy",
            "NFLX absent from historical source",
            "no historical executable option quotes or sector ETFs",
            "corporate-action adjustment quality and survivorship unverified",
            "fixed-notional stock portfolios, no intraday stops, commission or tax",
            "paired SPY/QQQ allocation references, not fully invested buy-and-hold",
        ],
        "ledger_sha256": hashlib.sha256(serialized).hexdigest(),
        "scores": scores,
    }
    # Round-trip date/tuple serialization into the canonical JSON boundary before hashing.
    normalized: dict[str, Any] = json.loads(_json(body))
    normalized["report_hash"] = report.checksum(normalized)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "replays.jsonl.gz").write_bytes(serialized)
    (directory / "report.json").write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    return normalized
