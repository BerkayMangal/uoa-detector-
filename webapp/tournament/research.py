"""Frozen temporal selection and a JOINT best-of-grid resampling diagnostic."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import numpy as np

from webapp.alpha_lab.db import digest
from webapp.calibration.model import load_csv
from webapp.tournament.engine import Metrics, Simulation, simulate
from webapp.tournament.model import BoolArray, Config, FloatArray, grid, mask, prepare
from webapp.tournament.settings import settings


@dataclass(frozen=True)
class Score:
    config: Config
    train: tuple[Metrics, ...]
    validation: tuple[Metrics, ...]

    @property
    def eligible(self) -> bool:
        s = settings()
        groups = (self.train, self.validation)
        if any(
            m.unresolved or m.missing_marks or m.matched != m.closed or m.excess_usd is None
            for ms in groups
            for m in ms
        ):
            return False
        train_ok = (
            sum(m.closed for m in self.train) >= s.min_train_closed
            and sum(m.net_usd for m in self.train) > 0
            and sum(m.excess_usd or 0 for m in self.train) > 0
        )
        val = self.validation
        return (
            train_ok
            and sum(m.closed for m in val) >= s.min_validation_closed
            and all(m.closed >= s.min_fold_closed for m in val)
            and sum(m.net_usd > 0 and (m.excess_usd or 0) > 0 for m in val) >= s.min_positive_folds
            and sum(m.net_usd for m in val) > 0
            and sum(m.excess_usd or 0 for m in val) > 0
            and max(m.max_drawdown_pct for m in val) <= s.max_drawdown_pct
        )

    @property
    def rank(self) -> tuple[float, float, float, str]:
        values = [m.excess_usd or 0 for m in self.validation]
        return (
            -float(np.median(values)),
            -float(np.mean(values)),
            max(m.max_drawdown_pct for m in self.validation),
            self.config.id,
        )


def selections(scores: tuple[Score, ...]) -> tuple[Score, ...]:
    families = settings().thresholds
    return tuple(
        min((x for x in scores if x.config.family == f and x.eligible), key=lambda x: x.rank)
        for f in families
        if any(x.config.family == f and x.eligible for x in scores)
    )


def test_pass(m: Metrics) -> bool:
    s = settings()
    return (
        m.closed >= s.min_test_closed
        and m.unresolved == 0
        and m.missing_marks == 0
        and m.matched == m.closed
        and m.net_usd > 0
        and m.excess_usd is not None
        and m.excess_usd > 0
        and m.max_drawdown_pct <= s.max_drawdown_pct
    )


def bootstrap(daily: FloatArray, lengths: tuple[int, ...]) -> FloatArray:
    s = settings()
    if (
        daily.ndim != 2
        or daily.shape[1] != sum(lengths)
        or not np.isfinite(daily).all()
        or any(n <= 0 for n in lengths)
    ):
        raise ValueError("complete aligned daily excess series required")
    rng = np.random.default_rng(s.bootstrap_seed)
    centered = daily - daily.mean(axis=1, keepdims=True)
    weights = np.zeros((sum(lengths), s.bootstrap_reps))
    for rep in range(s.bootstrap_reps):
        offset = 0
        for n in lengths:
            # Circular moving blocks, resampled inside each fold. ALL strategies
            # receive the identical sampled calendar dates in this replicate.
            picks: list[int] = []
            while len(picks) < n:
                start = int(rng.integers(0, n))
                picks.extend((start + j) % n for j in range(s.bootstrap_block_sessions))
            for j in picks[:n]:
                weights[offset + j, rep] += 1 / sum(lengths)
            offset += n
    return cast(FloatArray, np.max(centered @ weights, axis=0))


def _serialized(sim: Simulation) -> dict[str, Any]:
    # The report boundary is deliberately JSON-shaped; engine models are typed.
    return {
        "metrics": asdict(sim.metrics),
        "ledger": [asdict(t) for t in sim.trades],
        "equity": sim.equity,
        "daily_excess": sim.daily_excess,
    }


def run(source: Path, flow_source: Path) -> dict[str, Any]:
    from webapp.tournament.report import code_hash

    s = settings()
    p = prepare(load_csv(source))
    configs = grid()
    masks: dict[tuple[str, int, float, float, str], BoolArray] = {}
    scores = []
    daily = []
    lengths = tuple(sum(f.start <= d <= f.end for d in p.days) for f in s.folds)
    for i, c in enumerate(configs):
        selected = (
            masks.setdefault(c.mask_key, mask(p, c))
            if c.mask_key not in masks
            else masks[c.mask_key]
        )
        train = tuple(
            simulate(p, c, selected, s.train_start, f.train_end, s.slippage_bps).metrics
            for f in s.folds
        )
        val = tuple(simulate(p, c, selected, f.start, f.end, s.slippage_bps) for f in s.folds)
        scores.append(Score(c, train, tuple(v.metrics for v in val)))
        daily.append(tuple(x for v in val for x in v.daily_excess))
        if i % 500 == 0:
            print(f"scored {i}/{len(configs)}", flush=True)
    frozen = selections(tuple(scores))
    overall = min(frozen, key=lambda x: x.rank).config.id if frozen else None
    # Selection is frozen above; no internal test data enters that function.
    null = bootstrap(np.asarray(daily, dtype=np.float64), lengths)
    flow: dict[tuple[str, date], tuple[int, float]] = {}
    with flow_source.open() as handle:
        for row in csv.DictReader(handle):
            key = row["ticker"], date.fromisoformat(row["day"])
            if key in flow:
                raise ValueError("duplicate historical flow key")
            flow[key] = (int(row["print_count"]), float(row["mean_score"]))
    flow_known = np.zeros(p.open.shape, dtype=np.bool_)
    flow_good = flow_known.copy()
    for i, d in enumerate(p.days):
        for j, t in enumerate(p.tickers):
            item = flow.get((t, d))
            if item is not None:
                flow_known[i, j] = True
                flow_good[i, j] = item[0] >= s.flow_count_min and item[1] >= s.flow_score_min
    winners = []
    for score in frozen:
        c = score.config
        selected = masks[c.mask_key]
        observed = float(np.mean(daily[configs.index(c)]))
        primary = simulate(p, c, selected, s.test_start, s.test_end, s.slippage_bps, ledger=True)
        stages = {
            "train": [
                _serialized(
                    simulate(
                        p, c, selected, s.train_start, f.train_end, s.slippage_bps, ledger=True
                    )
                )
                for f in s.folds
            ],
            "validation": [
                _serialized(simulate(p, c, selected, f.start, f.end, s.slippage_bps, ledger=True))
                for f in s.folds
            ],
        }
        sensitivities = {
            str(bp): _serialized(
                simulate(p, c, selected, s.test_start, s.test_end, bp, ledger=True)
            )
            for bp in s.sensitivity_bps
        }
        overlap = {
            "known": _serialized(
                simulate(
                    p,
                    c,
                    selected & flow_known,
                    s.test_start,
                    s.test_end,
                    s.slippage_bps,
                    ledger=True,
                )
            ),
            "confirmed": _serialized(
                simulate(
                    p,
                    c,
                    selected & flow_good,
                    s.test_start,
                    s.test_end,
                    s.slippage_bps,
                    ledger=True,
                )
            ),
            "eligible_signals": int(
                np.sum(
                    selected[[i for i, d in enumerate(p.days) if s.test_start <= d <= s.test_end]]
                )
            ),
            "known_signals": int(
                np.sum(
                    (selected & flow_known)[
                        [i for i, d in enumerate(p.days) if s.test_start <= d <= s.test_end]
                    ]
                )
            ),
        }
        winners.append(
            {
                "id": c.id,
                "config": asdict(c),
                "rank": score.rank,
                "paper_enabled": test_pass(primary.metrics),
                "test": _serialized(primary),
                "bootstrap_p": (1 + int(np.sum(null >= observed))) / (s.bootstrap_reps + 1),
                "validation_mean_daily_excess": observed,
                "stages": stages,
                "sensitivity": sensitivities,
                "flow_overlap": overlap,
            }
        )
    import hashlib

    body = {
        "version": s.version,
        "evidence": "EXPLORATORY_REUSED_DATA_NO_VALIDATED_ALPHA",
        "profile_hash": s.profile_hash,
        "code_hash": code_hash(),
        "source_hashes": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (source, flow_source)
        },
        "tickers": p.tickers,
        "sessions": len(p.days),
        "rows": len(load_csv(source)),
        "configuration_trials": len(configs),
        "distinct_portfolio_evaluations": len(configs) * 6 + len(winners) * 5,
        "ledger_replays": len(winners) * 6,
        "eligible": sum(x.eligible for x in scores),
        "overall_id": overall,
        "prospective_start": s.prospective_start,
        "bootstrap": {
            "replicates": s.bootstrap_reps,
            "block_sessions": s.bootstrap_block_sessions,
            "seed": s.bootstrap_seed,
            "max_null_means": null.tolist(),
            "percentile": s.bootstrap_percentile,
            "threshold": float(np.percentile(null, s.bootstrap_percentile)),
            "fold_lengths": lengths,
        },
        "access": {
            "ohlcv": "17640 reused Study F rows",
            "historic_flow": f"{len(flow)} sparse Study E rows; absence unknown",
            "options": "dated replay snapshots only; no complete multi-session option-price panel",
            "uw_theta_local": "credentials absent; no historical vendor calls executed here",
            "public_history": "Yahoo/Stooq proxy HTTP 403; no public history fetched",
            "synthetic_and_reserved": "excluded; Study G untouched",
            "live_api_context": "prospective existing cached alerts and M21-M27 telemetry/news; no new requests",
        },
        "winners": winners,
        "grid": [
            {
                "id": x.config.id,
                "config": asdict(x.config),
                "eligible": x.eligible,
                "train": [asdict(m) for m in x.train],
                "validation": [asdict(m) for m in x.validation],
            }
            for x in scores
        ],
    }
    return body | {"report_hash": digest(body)}
