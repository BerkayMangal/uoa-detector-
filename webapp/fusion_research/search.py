"""Finite training-only selection with explicit shared temporal search accounting."""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np

from uoa_detector.live_alpha.calendar import close_time
from webapp.calibration.settings import calendar
from webapp.fusion_research import features
from webapp.fusion_research.features import FloatArray, IntArray, Prepared
from webapp.fusion_research.model import Candidate, OutcomeRow, digest, grid, ticker_estimates
from webapp.fusion_research.replay import Metrics, Outcomes, Unit, outcomes, replay
from webapp.fusion_research.settings import identity, settings

# Any is restricted to the bounded JSON report/plan adapter, not signal inputs.


def split_manifest(days: tuple[date, ...]) -> dict[str, Any]:
    cfg = settings()
    required = cfg.purge_sessions + cfg.embargo_sessions
    folds = []
    for fold in cfg.folds:
        training = tuple(d for d in days if cfg.source_start <= d <= fold.train_end)
        validation = tuple(d for d in days if fold.validation_start <= d <= fold.validation_end)
        gap = tuple(d for d in days if fold.train_end < d < fold.validation_start)
        if not training or not validation or len(gap) < required:
            raise ValueError("missing split support or insufficient session purge/embargo")
        folds.append(
            {
                "train_start": str(training[0]),
                "train_end": str(training[-1]),
                "validation_start": str(validation[0]),
                "validation_end": str(validation[-1]),
                "gap_sessions": [str(d) for d in gap],
                "required_gap_sessions": required,
            }
        )
    final = tuple(d for d in days if cfg.final_start <= d <= cfg.final_end)
    gap = tuple(d for d in days if cfg.final_train_end < d < cfg.final_start)
    if not final or len(gap) < required:
        raise ValueError("final split missing or insufficient purge/embargo")
    return {
        "version": "fusion_split_v1",
        "whole_date_shared_across_tickers": True,
        "training_only_selection": True,
        "folds": folds,
        "final_train_end": str(cfg.final_train_end),
        "final_start": str(final[0]),
        "final_end": str(final[-1]),
        "final_gap_sessions": [str(d) for d in gap],
        "purge_sessions": cfg.purge_sessions,
        "embargo_sessions": cfg.embargo_sessions,
    }


def plan(source: Path) -> dict[str, Any]:
    # No return calculation; profile/grid/splits are sealed first by the CLI.
    cfg = settings()
    path = features.source_path(source)
    bars = features.load(path)
    cal = calendar()
    spy_days = [b.day for b in bars if b.ticker == "SPY"]
    days = []
    d = min(spy_days)
    from uoa_detector.live_alpha.calendar import is_session_day

    while d <= max(spy_days):
        if is_session_day(d, cal) is True:
            days.append(d)
        d += timedelta(days=1)
    candidates = grid()
    body = {
        "version": cfg.version,
        "registered_at": datetime.now(UTC).isoformat(),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_window": [str(cfg.source_start), str(cfg.source_end)],
        "source_rows": len(bars),
        "source_tickers": sorted({b.ticker for b in bars}),
        "classification": "EXPLORATORY_REUSE",
        "candidate_count": len(candidates),
        "candidate_keys": [c.key for c in candidates],
        "configuration_cap": cfg.configuration_cap,
        "maximum_run_seconds": cfg.max_run_seconds,
        "split_manifest": split_manifest(tuple(days)),
        **identity(),
    }
    if len(candidates) != cfg.registered_configurations or len({c.key for c in candidates}) != len(
        candidates
    ):
        raise ValueError("registered candidates are not unique")
    return body | {"plan_hash": digest(body)}


def supported(m: Metrics, validation: bool = False) -> bool:
    cfg = settings()
    return (
        m.missing == 0
        and m.resolved >= (cfg.minimum_validation_units if validation else cfg.minimum_train_units)
        and m.days >= (cfg.minimum_validation_days if validation else cfg.minimum_train_days)
    )


def select_training(rows: tuple[tuple[Candidate, Metrics], ...]) -> Candidate | None:
    eligible = [
        (c, m)
        for c, m in rows
        if supported(m)
        and m.net_usd is not None
        and m.net_usd > 0
        and m.excess_usd is not None
        and m.excess_usd > 0
    ]
    return (
        min(
            eligible,
            key=lambda item: (
                -(item[1].mean_daily_excess_usd or 0),
                -(item[1].net_usd or 0),
                item[0].key,
            ),
        )[0]
        if eligible
        else None
    )


def joint_resampling(daily: FloatArray, lengths: tuple[int, ...]) -> dict[str, Any]:
    cfg = settings()
    if daily.ndim != 2 or daily.shape[1] != sum(lengths) or any(n < 1 for n in lengths):
        raise ValueError("joint null requires shared full-date folds")
    complete = np.all(np.isfinite(daily), axis=1)
    values = daily[complete]
    if not len(values):
        return {
            "state": "NO_COMPLETE_VECTORS",
            "registered_vectors": len(daily),
            "complete_vectors": 0,
            "draws": 0,
            "formal_significance_claim": False,
        }
    unique = np.unique(values, axis=0)
    centered = unique - np.mean(unique, axis=1, keepdims=True)
    rng = np.random.default_rng(cfg.bootstrap_seed)
    weights = np.zeros((sum(lengths), cfg.bootstrap_draws), dtype=np.float64)
    for rep in range(cfg.bootstrap_draws):
        offset = 0
        for n in lengths:
            picks: list[int] = []
            while len(picks) < n:
                start = int(rng.integers(0, n))
                picks.extend((start + j) % n for j in range(cfg.bootstrap_block_sessions))
            for j in picks[:n]:
                weights[offset + j, rep] += 1 / sum(lengths)
            offset += n
    null = np.max(centered @ weights, axis=0)
    return {
        "state": "DESCRIPTIVE_SHARED_DATE_MAXIMUM",
        "registered_vectors": len(daily),
        "complete_vectors": len(values),
        "incomplete_vectors": int(np.sum(~complete)),
        "unique_return_vectors": len(unique),
        "draws": cfg.bootstrap_draws,
        "block_sessions": cfg.bootstrap_block_sessions,
        "seed": cfg.bootstrap_seed,
        "quantile": cfg.bootstrap_percentile,
        "maximum_centered_mean_quantile_usd": float(np.percentile(null, cfg.bootstrap_percentile)),
        "maximum_centered_mean_draws_usd": [float(v) for v in null],
        "resampling_shared_across_all_candidates": True,
        "formal_significance_claim": False,
        "limitation": "Reused historical data; diagnostic maximum across complete fixed-candidate validation vectors, not a p-value for adaptive fold training or prospective alpha.",
    }


def _label(p: Prepared, c: Candidate, u: Unit) -> OutcomeRow:
    cal = calendar()
    zone = ZoneInfo(cal.timezone)
    entry = datetime.combine(u.entry_day, cal.regular_open, zone).astimezone(UTC)
    exit_ = datetime.combine(u.exit_day, close_time(u.exit_day, cal), zone).astimezone(UTC)
    entry_known = entry + timedelta(minutes=1)
    known = exit_ + timedelta(minutes=1)
    return OutcomeRow(
        event_id=digest((c.key, u.ticker, u.signal_day)),
        ticker=u.ticker,
        session=u.signal_day,
        method="exploratory_daily_next_open_close_v1",
        instrument="stock_reference" if c.direction == "up" else "short_stock_mirror_diagnostic",
        horizon=f"{c.horizon}sessions",
        state="observed" if not u.missing else "missing",
        entry_at=entry,
        exit_at=exit_,
        entry_known_at=entry_known,
        exit_known_at=known,
        mature_at=known,
        known_at=known,
        net_usd=u.net_usd,
        excess_usd=u.excess_usd,
        missing=(u.missing,) if u.missing else (),
    )


def _diagnostics(units: tuple[Unit, ...]) -> dict[str, Any]:
    valid = [u for u in units if u.net_usd is not None]
    ranked = sorted(valid, key=lambda u: -(u.net_usd or 0))
    ticker: dict[str, float] = defaultdict(float)
    days: dict[str, float] = defaultdict(float)
    for u in valid:
        ticker[u.ticker] += u.net_usd or 0
        days[str(u.signal_day)] += u.net_usd or 0
    return {
        "without_best_1_net_usd": sum(u.net_usd or 0 for u in ranked[1:]),
        "without_best_3_net_usd": sum(u.net_usd or 0 for u in ranked[3:]),
        "ticker_net_usd": dict(sorted(ticker.items())),
        "signal_day_net_usd": dict(sorted(days.items())),
        "ticker_unit_counts": dict(Counter(u.ticker for u in valid)),
        "source_days": len(days),
        "units": len(valid),
        "not_independent_bets": True,
    }


def run(
    source: Path,
    registered_plan: dict[str, Any],
    *,
    progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, Any]:
    cfg = settings()
    start = time.monotonic()
    path = features.source_path(source)
    if (
        registered_plan.get("source_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        or any(registered_plan.get(k) != v for k, v in identity().items())
        or digest({k: v for k, v in registered_plan.items() if k != "plan_hash"})
        != registered_plan.get("plan_hash")
    ):
        raise ValueError("plan/source/code changed after registration")
    candidates = grid()
    if registered_plan.get("candidate_keys") != [c.key for c in candidates]:
        raise ValueError("candidate registry changed")
    p = features.prepare(features.load(path))
    splits = split_manifest(p.days)
    if registered_plan.get("split_manifest") != splits:
        raise ValueError("split manifest changed")
    rosters: dict[tuple[str, int, float, float, str, str], tuple[IntArray, IntArray]] = {}
    price_cache: dict[tuple[str, int, float, bool], Outcomes] = {}

    def mark(c: Candidate, bps: float = cfg.cost_bps_per_side, atr: bool = False) -> Outcomes:
        key = c.direction, c.horizon, bps, atr
        if key not in price_cache:
            price_cache[key] = outcomes(p, c.direction, c.horizon, bps, atr_sizing=atr)
        return price_cache[key]

    def roster(c: Candidate) -> tuple[IntArray, IntArray]:
        if c.mask_key not in rosters:
            rosters[c.mask_key] = features.roster(p, c)
        return rosters[c.mask_key]

    rows = []
    fold_training: list[list[tuple[Candidate, Metrics]]] = [[] for _ in cfg.folds]
    full_training = []
    daily = []
    identities = set()
    missing_cells = zero_cells = 0
    replays = 0
    lengths = tuple(
        sum(f.validation_start <= d <= f.validation_end for d in p.days) for f in cfg.folds
    )
    for index, c in enumerate(candidates):
        if time.monotonic() - start > cfg.max_run_seconds:
            raise TimeoutError("registered research CPU/wall budget exhausted")
        selected_roster = roster(c)
        o = mark(c)
        folds = []
        vector = []
        for j, f in enumerate(cfg.folds):
            tr = replay(p, c, selected_roster, o, cfg.source_start, f.train_end)
            va = replay(p, c, selected_roster, o, f.validation_start, f.validation_end)
            fold_training[j].append((c, tr.metrics))
            folds.append(
                {
                    "training": asdict(tr.metrics),
                    "validation": asdict(va.metrics),
                    "training_supported": supported(tr.metrics),
                    "validation_supported": supported(va.metrics, True),
                }
            )
            vector.extend(va.daily_excess.tolist())
            identities.add(va.ledger_identity)
            replays += 2
        train = replay(p, c, selected_roster, o, cfg.source_start, cfg.final_train_end)
        full_training.append((c, train.metrics))
        replays += 1
        ms = [train.metrics, *[m for _, m in [fold_training[j][-1] for j in range(len(cfg.folds))]]]
        missing_cells += int(any(m.missing for m in ms))
        zero_cells += int(not train.metrics.selected)
        rows.append(
            {
                "key": c.key,
                "candidate": asdict(c),
                "folds": folds,
                "full_training": asdict(train.metrics),
                "inapplicable": False,
                "historical_missing_modalities": [
                    "news_semantics",
                    "point_in_time_fundamentals",
                    "event_time_option_flow",
                    "gamma_darkpool_iv_sector_receipts",
                ],
            }
        )
        daily.append(vector)
        if progress and (index % 256 == 0 or index + 1 == len(candidates)):
            progress(
                {
                    "completed_configurations": index + 1,
                    "registered_configurations": len(candidates),
                    "executed_reference_replays": replays,
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                }
            )
    selected_folds = []
    ticker_report = []
    for j, f in enumerate(cfg.folds):
        chosen_fold = select_training(tuple(fold_training[j]))
        body: dict[str, Any] = {
            "fold": j,
            "selected_by": "training_only",
            "candidate_key": chosen_fold.key if chosen_fold else None,
        }
        if chosen_fold:
            tr = replay(
                p,
                chosen_fold,
                roster(chosen_fold),
                mark(chosen_fold),
                cfg.source_start,
                f.train_end,
                ledger=True,
            )
            va = replay(
                p,
                chosen_fold,
                roster(chosen_fold),
                mark(chosen_fold),
                f.validation_start,
                f.validation_end,
                ledger=True,
            )
            replays += 2
            body.update(
                candidate=asdict(chosen_fold),
                training=asdict(tr.metrics),
                validation=asdict(va.metrics),
                validation_supported=supported(va.metrics, True),
            )
            if j == len(cfg.folds) - 1:
                tr_labels = tuple(_label(p, chosen_fold, u) for u in tr.ledger)
                va_labels = tuple(_label(p, chosen_fold, u) for u in va.ledger)
                at = datetime.combine(cfg.final_start, day_time.min, UTC)
                ticker_report = [asdict(e) for e in ticker_estimates(tr_labels, va_labels, at)]
        selected_folds.append(body)
    selected = select_training(tuple(full_training))
    fit = None
    ledger = []
    controls = []
    if selected:
        final = replay(
            p,
            selected,
            roster(selected),
            mark(selected),
            cfg.final_start,
            cfg.final_end,
            ledger=True,
        )
        replays += 1
        fit = {
            "candidate": asdict(selected),
            "key": selected.key,
            "selected_by": "training_only_through_final_train_end",
            "training": asdict(next(m for c, m in full_training if c.key == selected.key)),
            "final_diagnostic": asdict(final.metrics),
            "final_supported": supported(final.metrics, True),
            "diagnostics": _diagnostics(final.ledger),
            "not_promoted": True,
        }
        ledger = [asdict(u) for u in final.ledger]
        for bps in cfg.stress_bps_per_side:
            control = replay(
                p, selected, roster(selected), mark(selected, bps), cfg.final_start, cfg.final_end
            )
            replays += 1
            controls.append(
                {
                    "variant": "cost_stress",
                    "bps_per_side": bps,
                    "sizing": "whole_units_recomputed_under_same_affordability_rule",
                    "metrics": asdict(control.metrics),
                }
            )
        control = replay(
            p, selected, roster(selected), mark(selected, atr=True), cfg.final_start, cfg.final_end
        )
        replays += 1
        controls.append(
            {
                "variant": "atr_budget_sizing",
                "risk_usd": cfg.risk_usd,
                "risk_atr_multiple": cfg.risk_atr_multiple,
                "metrics": asdict(control.metrics),
            }
        )
    joint = joint_resampling(cast(FloatArray, np.array(daily, dtype=np.float64)), lengths)
    body = {
        "version": cfg.version,
        "classification": "EXPLORATORY_REUSE",
        "status": "EXPLORATORY_COMPLETE",
        "winner": None,
        "live_promotion": False,
        "validated_alpha": False,
        "inference": "NOT_READY",
        "fresh_sessions_in_this_dataset": 0,
        "minimum_prospective_sessions": cfg.minimum_prospective_sessions,
        "readiness_reasons": [
            "All historical dates were already searched; no unseen validation claimed.",
            "Historical semantic-news and point-in-time fundamental labels are unavailable.",
            "No new live rule or per-ticker success probability is promoted.",
        ],
        "source_sha256": registered_plan["source_sha256"],
        "plan_hash": registered_plan["plan_hash"],
        "split_manifest": splits,
        **identity(),
        "source_rows": registered_plan["source_rows"],
        "source_tickers": list(p.tickers),
        "source_sessions": len(p.days),
        "configuration_trials": len(candidates),
        "configuration_cap": cfg.configuration_cap,
        "reference_replay_evaluations": replays,
        "cached_rosters": len(rosters),
        "distinct_validation_roster_horizons": len(identities),
        "zero_selection_configurations": zero_cells,
        "missing_training_configurations": missing_cells,
        "inapplicable_configurations": 0,
        "joint_resampling": joint,
        "training_selected_folds": selected_folds,
        "exploratory_fit": fit,
        "selected_controls": controls,
        "selected_ledger": ledger,
        "ticker_estimates": ticker_report,
        "ticker_estimate_scope": "Last fold training-selected candidate; training labels only, independent validation support counts, global fallback when60/20units or20/10days absent.",
        "grid": rows,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "limits": [
            "Independent2500USD reference units with4new units/day; not a cash portfolio.",
            "Daily1/3/5-session labels are separate from15/60-minute last-trade captures.",
            "Short-stock mirrors exclude borrow/locate/dividend/margin/availability costs.",
            "Historical next-open/close knowability is assumed; no historical physical receipts or executable fills claimed.",
            "Current constituent/survivorship and corporate-action adjustment quality remain limitations.",
            "No fundamental/news/flow weight is fitted from unavailable historical features.",
        ],
    }
    return body | {"report_hash": digest(body)}
