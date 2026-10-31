"""A finite walk-forward audit. Selection never reads the final window."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from statistics import mean
from typing import Any

from webapp.event_calibration.features import build
from webapp.event_calibration.instruments import compare
from webapp.event_calibration.model import Dataset, digest, identity, settings
from webapp.event_calibration.replay import Candidate, Replay, candidates, replay
from webapp.event_calibration.series import Series


def summary(r: Replay, days: tuple[date, ...]) -> dict[str, Any]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for u in r.units:
        grouped[u.day].append(u.excess_pp)
    selected_by_day = Counter(r.selected_days)
    daily = [
        None if len(grouped[d]) < selected_by_day[d] else mean(grouped[d]) if grouped[d] else 0.0
        for d in days
    ]
    ordered = sorted((u.net_pct for u in r.units), reverse=True)
    return {
        "selected": r.selected,
        "resolved": len(r.units),
        "unresolved": r.unresolved,
        "days_with_units": len({u.day for u in r.units}),
        "boundary_excluded": r.boundary_excluded,
        "mean_net_pct": mean(u.net_pct for u in r.units) if r.units else None,
        "mean_daily_excess_pp": mean(v for v in daily if v is not None)
        if daily and all(v is not None for v in daily)
        else None,
        "win_fraction": mean(u.net_pct > 0 for u in r.units) if r.units else None,
        "sum_unit_pct_without_best_1": sum(ordered[1:]) if ordered else None,
        "sum_unit_pct_without_best_3": sum(ordered[3:]) if ordered else None,
        "ticker_units": dict(Counter(u.ticker for u in r.units)),
        "daily_excess_pp": daily,
    }


def supported(r: Replay, train: bool) -> bool:
    c = settings()
    return (
        not r.unresolved
        and len(r.units) >= (c.minimum_train_units if train else c.minimum_validation_units)
        and len({u.day for u in r.units})
        >= (c.minimum_train_days if train else c.minimum_validation_days)
    )


def forecast(train: Replay, control: Replay) -> dict[str, float]:
    n = len(train.units)
    weight = settings().shrinkage_units
    prior_p = mean(u.net_pct > 0 for u in control.units)
    prior_ev = mean(u.net_pct for u in control.units)
    return {
        "p": (sum(u.net_pct > 0 for u in train.units) + weight * prior_p) / (n + weight),
        "expected_net_pct": (sum(u.net_pct for u in train.units) + weight * prior_ev)
        / (n + weight),
        "training_units": float(n),
        "prior_p": prior_p,
    }


def _cut(day: date) -> datetime:
    # Conservative full UTC-day embargo; the frozen regular session is later.
    return datetime.combine(day, datetime.min.time(), UTC)


def run(data: Dataset) -> dict[str, Any]:
    c = settings()
    roster = candidates()
    samples = build(data)
    series = Series.make(data)
    availability = {
        "raw_decisions": len(data.decisions),
        "first_bucket_samples": len(samples),
        "prices": len(data.prices),
        "alerts": len(data.alerts),
        "polls": len(data.polls),
        "sessions": len(data.sessions),
        "complete_flow_samples": sum(s.up is not None for s in samples),
        "normalized_samples": sum(s.ratio is not None for s in samples),
        "price_acceptance_samples": sum(s.acceptance is not None for s in samples),
        "news_known_samples": sum(s.decision.news is not None for s in samples),
        "market_known_samples": sum(s.market is not None for s in samples),
        "sector_known_samples": sum(s.decision.sector is not None for s in samples),
        "gamma_known_samples": sum(s.decision.gamma is not None for s in samples),
        "quote_packets": sum(bool(s.decision.options) for s in samples),
    }
    report: dict[str, Any] = {
        "version": c.version,
        "classification": "EXPLORATORY_OBSERVED_REFERENCE_UNITS",
        "status": "DATA_BLOCKED",
        "validated_alpha": False,
        "live_promotion": False,
        "planned_candidates": len(roster),
        "executed_replays": 0,
        "availability": availability,
        "source_hash": digest(data.model_dump(mode="json")),
        "provenance": data.provenance,
        **identity(),
        "rows": [],
        "winner": None,
        "reasons": [],
    }
    if len(data.sessions) < c.minimum_sessions:
        report["reasons"] = [
            f"Need at least {c.minimum_sessions} observed eligible sessions; have {len(data.sessions)}. No fitted winner or probability was produced."
        ]
        return report
    dev, final = data.sessions[: c.development_sessions], data.sessions[c.development_sessions :]
    if len(final) < c.minimum_sessions - c.development_sessions:
        raise ValueError("final diagnostic too short")
    cache: dict[tuple[str, tuple[date, ...], datetime], Replay] = {}

    def evaluate(candidate: Candidate, days: tuple[date, ...], cutoff: datetime) -> Replay:
        # Memoize only development evaluations; final labels are not requested here.
        key = candidate.key, days, cutoff
        if key not in cache:
            cache[key] = replay(
                data, samples, candidate, days, c.slippage_bps[0], label_asof=cutoff, index=series
            )
            report["executed_replays"] += 1
        return cache[key]

    contenders = []
    for candidate in roster:
        folds: list[dict[str, Any]] = []
        valid = True
        for n in c.training_sessions:
            train_days = dev[: n - c.embargo_sessions]
            validation_days = dev[n : n + c.validation_sessions]
            cutoff = _cut(validation_days[0])
            end_cutoff = (
                _cut(dev[n + c.validation_sessions])
                if n + c.validation_sessions < len(dev)
                else _cut(final[0])
            )
            train = evaluate(candidate, train_days, cutoff)
            validation = evaluate(candidate, validation_days, end_cutoff)
            control = evaluate(
                replace(candidate, arm="baseline", ratio=0, dominance=0, percentile=0),
                train_days,
                cutoff,
            )
            ok = (
                supported(train, True) and supported(control, True) and supported(validation, False)
            )
            valid &= ok
            fit = forecast(train, control) if ok else None
            score = summary(validation, validation_days)
            folds.append(
                {
                    "train": summary(train, train_days),
                    "validation": score,
                    "fit": fit,
                    "eligible": ok,
                    "brier": mean((fit["p"] - (u.net_pct > 0)) ** 2 for u in validation.units)
                    if fit and validation.units
                    else None,
                }
            )
        value = (
            mean(float(f["validation"]["mean_daily_excess_pp"]) for f in folds) if valid else None
        )
        report["rows"].append(
            {
                "key": candidate.key,
                "parameters": asdict(candidate),
                "eligible": valid,
                "selection_score_pp": value,
                "folds": folds,
            }
        )
        if value is not None:
            contenders.append((value, candidate))
    if not contenders:
        report["status"] = "NO_ELIGIBLE_CANDIDATE"
        report["reasons"] = [
            "No candidate met all frozen coverage and sample-support gates. Missing features and outcomes were not filled."
        ]
        return report
    _, winner = sorted(contenders, key=lambda pair: (-pair[0], pair[1].key))[0]
    # This selection receipt is calculated before any final-window outcome replay.
    report["selection_receipt"] = digest(
        {
            "winner": winner.key,
            "scores": [(r["key"], r["selection_score_pp"]) for r in report["rows"]],
            "source": report["source_hash"],
        }
    )
    train_days = dev[: -c.embargo_sessions]
    train = evaluate(winner, train_days, _cut(final[0]))
    control = evaluate(
        replace(winner, arm="baseline", ratio=0, dominance=0, percentile=0),
        train_days,
        _cut(final[0]),
    )
    if not supported(train, True) or not supported(control, True):
        report["status"] = "FINAL_TRAINING_UNSUPPORTED"
        report["reasons"] = [
            "Frozen winner retained; insufficient final-training support. No replacement selected."
        ]
        return report
    fit = forecast(train, control)
    result = replay(data, samples, winner, final, c.slippage_bps[0], index=series)
    report["executed_replays"] += 1
    metrics = summary(result, final)
    baseline_final = replay(
        data,
        samples,
        replace(winner, arm="baseline", ratio=0, dominance=0, percentile=0),
        final,
        c.slippage_bps[0],
        index=series,
    )
    report["executed_replays"] += 1
    report["baseline_final"] = summary(baseline_final, final)
    report["instrument_comparisons"] = [
        asdict(row) for unit in result.units for row in compare(data, unit)
    ]
    stresses = []
    for bps in c.slippage_bps:
        for delay in c.entry_delay_minutes:
            diagnostic = replay(
                data, samples, replace(winner, delay=delay), final, bps, index=series
            )
            report["executed_replays"] += 1
            stresses.append(
                {"bps_per_side": bps, "delay_minutes": delay, **summary(diagnostic, final)}
            )
    daily = metrics["daily_excess_pp"] if not result.unresolved else []
    rng = random.Random(c.bootstrap_seed)
    boot = []
    for _ in range(c.bootstrap_draws if daily else 0):
        draw: list[float] = []
        while len(draw) < len(daily):
            start = rng.randrange(len(daily))
            draw.extend(daily[(start + i) % len(daily)] for i in range(c.bootstrap_block_days))
        boot.append(mean(draw[: len(daily)]))
    boot.sort()
    report.update(
        status="FINAL_DATA_INCOMPLETE" if result.unresolved else "EXPLORATORY_CANDIDATE_ONLY",
        winner={"key": winner.key, "parameters": asdict(winner), "fit": fit},
        final={
            **metrics,
            "brier": mean((fit["p"] - (u.net_pct > 0)) ** 2 for u in result.units)
            if result.units
            else None,
            "training_prior_brier": mean(
                (fit["prior_p"] - (u.net_pct > 0)) ** 2 for u in result.units
            )
            if result.units
            else None,
            "observed_win_fraction": metrics["win_fraction"],
            "descriptive_daily_excess_interval": [
                boot[int(0.025 * (len(boot) - 1))],
                boot[int(0.975 * (len(boot) - 1))],
            ]
            if boot
            else None,
            "ledger": [asdict(u) for u in result.units],
        },
        stress=stresses,
        reasons=[
            "A fitted shrinkage estimate is not a validated success probability.",
            "All units are modeled observed-price references, not executions or a cash portfolio.",
            "No new winner is substituted after final diagnostics; no live promotion.",
        ],
    )
    return report
