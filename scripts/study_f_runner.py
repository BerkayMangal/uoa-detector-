"""Study F: the pre-registered search, its null, and nothing else.

    uv run --group research python scripts/study_f_runner.py \\
        data/study_f/bars.csv out/study_f_h1.jsonl --null 200

Protocol: ``docs/study-F-preregistration.md`` §4-§7, amended by
``docs/study-F-preregistration-addendum.md`` (the purge at the fold boundary).
Nothing here chooses a feature, a configuration or a threshold: all three were
frozen before this file existed, and §7 says a miss is a rejection rather than a
reason to retune.

Three things in this file are load-bearing and worth reading before trusting a
number it prints.

**The purge.** ``folds`` ends the training window ``horizon - 1`` sessions before
the test window opens. Without it the last training rows carry labels built from
test-window prices, and because the null permutes within each day it destroys the
cross-sectional content of exactly those leaked returns — so the leak would
inflate the real arm and leave the null it is measured against untouched. That
asymmetry is the one failure mode a shuffled-label control exists to rule out.

**The null takes its best too.** The real arm reports the best of ten
configurations, so each null pass reruns all ten and reports its best. A null that
scored one model would be a distribution of a different statistic, which is §8's
defect 5 — the reason this design exists.

**The cross-sectional feature.** ``xs_rank_ret_20`` cannot come from the feature
library: it is a rank across names on one day, and the library takes a single
ticker's prefix by construction so that no function can reach past day t. It is
computed here, from ``ret_20`` values that are themselves prefix-only, which keeps
the frozen §4 list complete at 37 without opening a path for look-ahead.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.research.features import Bar, feature_names, features

# Every constant below is frozen by the pre-registration.
TRAIN = 120            # §6 training sessions
TEST = 21              # §6 test sessions per fold
STEP = 21              # §6 roll
HORIZONS = (1, 5)      # §1
WARMUP = 60            # §3, the longest feature window
MARKET = "SPY"         # §4 family 7
MIN_DOLLAR_VOLUME = 20_000_000.0   # §3 liquidity filter, declared before fitting
ANALOG_K = 25          # §4 family 8
NULL_PASSES = 200      # §6
T1_PERCENTILE = 99.0   # §7
T2_POSITIVE_FOLD_SHARE = 0.75   # §7
SEED = 20260919

XS_FEATURE = "xs_rank_ret_20"
PANEL_FEATURES = (*feature_names(), XS_FEATURE)

# §5: eight XGBoost configurations, one ridge, one analog.
XGB_GRID = tuple(
    {"max_depth": depth, "n_estimators": trees, "learning_rate": rate}
    for depth in (2, 3)
    for trees in (100, 300)
    for rate in (0.03, 0.1)
)

LabelKind = Literal["forward", "idio"]


@dataclass(frozen=True)
class Panel:
    """One row per (day, ticker): features known at the close of t, label after t."""

    days: tuple[date, ...]
    tickers: tuple[str, ...]
    day_of: np.ndarray       # (n_rows,) index into days
    ticker_of: np.ndarray    # (n_rows,) ticker symbol, for T4's long-short book
    x: np.ndarray            # (n_rows, n_features)
    y: np.ndarray            # label, per ``label`` below
    label: LabelKind


def load_bars(path: pathlib.Path) -> dict[str, list[Bar]]:
    by_ticker: dict[str, list[Bar]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_ticker[row["ticker"]].append(Bar(
                day=date.fromisoformat(row["day"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return {
        ticker: sorted(bars, key=lambda bar: bar.day)
        for ticker, bars in by_ticker.items()
    }


def liquid_tickers(by_ticker: dict[str, list[Bar]]) -> list[str]:
    """§3: median daily dollar volume >= $20M. Fixed before any estimator ran."""
    keep = []
    for ticker, bars in by_ticker.items():
        median = float(np.median([bar.close * bar.volume for bar in bars]))
        if median >= MIN_DOLLAR_VOLUME:
            keep.append(ticker)
    return sorted(keep)


def build_panel(
    by_ticker: dict[str, list[Bar]], horizon: int, *, label: LabelKind = "forward",
) -> Panel:
    """Features from ``bars[:i+1]``; label from ``bars[i+horizon]``.

    The only indexing into the future in this function is the label. Features are
    produced by handing ``features`` a prefix, so a look-ahead would have to be
    written into the library, where its own tests forbid it.

    ``label="idio"`` is §7's T3 control: the forward return minus beta times the
    market's forward return, with beta taken from the same 60-session window the
    feature library uses — so T3 asks whether anything survives once the market
    factor is removed from the thing being predicted.
    """
    tickers = liquid_tickers(by_ticker)
    market = by_ticker[MARKET]
    market_row = {bar.day: index for index, bar in enumerate(market)}

    names = feature_names()

    day_of: list[date] = []
    ticker_of: list[str] = []
    xs: list[list[float]] = []
    ys: list[float] = []
    ret_20: list[float] = []
    for ticker in tickers:
        bars = by_ticker[ticker]
        for i in range(WARMUP, len(bars) - horizon):
            cut = market_row.get(bars[i].day)
            computed = features(bars[: i + 1], None if cut is None else market[: cut + 1])
            values = [computed[name] for name in names]
            if any(value is None or not math.isfinite(value) for value in values):
                continue
            later, now = bars[i + horizon].close, bars[i].close
            if later <= 0.0 or now <= 0.0:
                continue
            forward = math.log(later / now)
            if label == "idio":
                beta = computed["beta_60"]
                if beta is None or cut is None or cut + horizon >= len(market):
                    continue
                market_now, market_later = market[cut].close, market[cut + horizon].close
                if market_now <= 0.0 or market_later <= 0.0:
                    continue
                forward -= beta * math.log(market_later / market_now)
            day_of.append(bars[i].day)
            ticker_of.append(ticker)
            xs.append([float(value) for value in values])  # type: ignore[arg-type]
            ys.append(forward)
            ret_20.append(float(computed["ret_20"]))  # type: ignore[arg-type]

    if not xs:
        msg = "the panel is empty: every row lost a feature to a None"
        raise SystemExit(msg)
    # The day index enumerates sessions that CARRY ROWS, not the calendar. Indexing
    # the full 252 sessions would count the 60 warmup days — which have no rows —
    # inside the first training window, so "train 120 sessions" (§6) would really be
    # 60 populated ones. Study E's harness indexes its panel's own days; this must
    # too, or the two are not the comparison §2 claims they are.
    used = sorted(set(day_of))
    position = {day: index for index, day in enumerate(used)}
    matrix = np.asarray(xs, dtype=float)
    days_array = np.asarray([position[day] for day in day_of], dtype=int)
    cross = _cross_sectional_rank(np.asarray(ret_20, dtype=float), days_array)
    return Panel(
        days=tuple(used), tickers=tuple(tickers), day_of=days_array,
        ticker_of=np.asarray(ticker_of), x=np.column_stack([matrix, cross]),
        y=np.asarray(ys, dtype=float), label=label,
    )


def _cross_sectional_rank(values: np.ndarray, day_of: np.ndarray) -> np.ndarray:
    """``xs_rank_ret_20``: where a name's 20-day return sits among that day's names.

    Scaled to [0, 1] so the number does not depend on how many names traded that
    day. Only same-day values enter, which is what makes this usable at t.
    """
    out = np.empty(len(values), dtype=float)
    for day in np.unique(day_of):
        mask = day_of == day
        ranks = _ranks(values[mask])
        count = int(mask.sum())
        out[mask] = 0.5 if count == 1 else ranks / (count - 1)
    return out


def _ranks(values: np.ndarray) -> np.ndarray:
    """Average ranks, ties shared. Written out because scipy is not a hard dep."""
    order = values.argsort(kind="stable")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values), dtype=float)
    for value in np.unique(values):
        tied = values == value
        if int(tied.sum()) > 1:
            ranks[tied] = ranks[tied].mean()
    return ranks


def folds(n_days: int, horizon: int) -> list[tuple[range, range]]:
    """Rolling walk-forward, train 120 / test 21 / step 21, with the purge.

    The addendum amends §6 on one point: the training window ends
    ``horizon - 1`` sessions before the test window opens, so no training label's
    window reaches into the test fold. A no-op at horizon 1; drops the last four
    training days at horizon 5.
    """
    purge = horizon - 1
    out: list[tuple[range, range]] = []
    start = TRAIN
    while start + TEST <= n_days:
        out.append((range(start - TRAIN, start - purge), range(start, start + TEST)))
        start += STEP
    return out


def standardise(train: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """§6: fitted on the training window only, applied to the test window unchanged."""
    mean = train.mean(axis=0)
    sd = train.std(axis=0)
    sd[sd == 0.0] = 1.0
    return (train - mean) / sd, (test - mean) / sd


def rank_within_day(matrix: np.ndarray, day_of: np.ndarray) -> np.ndarray:
    """Every column replaced by its cross-sectional rank on that day, in [0, 1].

    §5's ridge is specified on cross-sectionally ranked features. Ranking is
    per-day and therefore carries nothing across the split boundary — it needs no
    training statistics at all, which is why it is not routed through
    ``standardise``.
    """
    out = np.empty_like(matrix)
    for day in np.unique(day_of):
        mask = day_of == day
        count = int(mask.sum())
        block = matrix[mask]
        if count == 1:
            out[mask] = 0.5
            continue
        for column in range(matrix.shape[1]):
            out[np.flatnonzero(mask), column] = _ranks(block[:, column]) / (count - 1)
    return out


def spearman(predicted: np.ndarray, realised: np.ndarray) -> float | None:
    """Spearman rank correlation, or None when the day cannot produce one."""
    if predicted.size < 3:
        return None
    a = _ranks(predicted) - _ranks(predicted).mean()
    b = _ranks(realised) - _ranks(realised).mean()
    denominator = math.sqrt(float((a**2).sum()) * float((b**2).sum()))
    if denominator <= 0.0:
        return None
    return float((a * b).sum() / denominator)


def configurations() -> list[dict[str, object]]:
    """The ten of §5, in a fixed order."""
    out: list[dict[str, object]] = [{"kind": "xgb", **config} for config in XGB_GRID]
    out.append({"kind": "ridge"})
    out.append({"kind": "analog"})
    return out


def label_of(config: dict[str, object]) -> str:
    if config["kind"] != "xgb":
        return str(config["kind"])
    return (f"xgb_d{config['max_depth']}_n{config['n_estimators']}"
            f"_lr{config['learning_rate']}")


def analog_predict(
    train_x: np.ndarray, train_y: np.ndarray, test_x: np.ndarray, k: int = ANALOG_K,
) -> np.ndarray:
    """§4 family 8: mean forward return of the k nearest TRAINING rows, cosine.

    Neighbours are drawn from ``train_x`` alone. There is no path by which a test
    row can become its own neighbour, which is §8's defect 2.
    """
    train_norm = np.linalg.norm(train_x, axis=1)
    train_norm[train_norm == 0.0] = 1.0
    test_norm = np.linalg.norm(test_x, axis=1)
    test_norm[test_norm == 0.0] = 1.0
    similarity = (test_x / test_norm[:, None]) @ (train_x / train_norm[:, None]).T
    take = min(k, train_x.shape[0])
    nearest = np.argpartition(-similarity, kth=take - 1, axis=1)[:, :take]
    return np.asarray([train_y[row].mean() for row in nearest], dtype=float)


def predict(
    config: dict[str, object], train_x: np.ndarray, train_y: np.ndarray,
    test_x: np.ndarray, *, train_days: np.ndarray, test_days: np.ndarray, seed: int,
) -> np.ndarray:
    kind = config["kind"]
    if kind == "xgb":
        from xgboost import XGBRegressor
        model = XGBRegressor(
            max_depth=config["max_depth"], n_estimators=config["n_estimators"],
            learning_rate=config["learning_rate"], subsample=0.8,
            colsample_bytree=0.8, min_child_weight=5, random_state=seed,
            n_jobs=1, verbosity=0, tree_method="hist",
        )
        model.fit(train_x, train_y)
        return np.asarray(model.predict(test_x), dtype=float)
    if kind == "ridge":
        from sklearn.linear_model import Ridge
        model = Ridge(alpha=1.0)
        model.fit(rank_within_day(train_x, train_days), train_y)
        return np.asarray(model.predict(rank_within_day(test_x, test_days)), dtype=float)
    return analog_predict(train_x, train_y, test_x)


@dataclass(frozen=True)
class SearchResult:
    """One pass of the ten-configuration search."""

    mean_ic: dict[str, float]          # configuration -> mean OOS rank IC
    per_fold: dict[str, list[float]]   # configuration -> IC per fold

    @property
    def best(self) -> str:
        return max(self.mean_ic, key=lambda name: self.mean_ic[name])

    @property
    def best_ic(self) -> float:
        return self.mean_ic[self.best]

    def positive_fold_share(self, config: str) -> float:
        ics = self.per_fold[config]
        return sum(1 for ic in ics if ic > 0.0) / len(ics) if ics else 0.0


def shuffle_within_day(y: np.ndarray, day_of: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """§6's null: permute labels within each day.

    The cross-section, the market factor and every feature survive; only the
    pairing of a row to its outcome is destroyed.
    """
    out = y.copy()
    for day in np.unique(day_of):
        mask = day_of == day
        out[mask] = rng.permutation(out[mask])
    return out


def run_search(
    panel: Panel, horizon: int, *, shuffle: bool, rng: np.random.Generator,
) -> SearchResult:
    y = shuffle_within_day(panel.y, panel.day_of, rng) if shuffle else panel.y
    per_fold: dict[str, list[float]] = defaultdict(list)
    for train_days, test_days in folds(len(panel.days), horizon):
        in_train = np.isin(panel.day_of, np.fromiter(train_days, dtype=int))
        in_test = np.isin(panel.day_of, np.fromiter(test_days, dtype=int))
        if int(in_train.sum()) < 50 or int(in_test.sum()) < 10:
            continue
        train_x, test_x = standardise(panel.x[in_train], panel.x[in_test])
        train_y, test_y = y[in_train], y[in_test]
        test_day_of = panel.day_of[in_test]
        for index, config in enumerate(configurations()):
            predicted = predict(
                config, train_x, train_y, test_x,
                train_days=panel.day_of[in_train], test_days=test_day_of,
                seed=SEED + index,
            )
            daily = [
                ic for day in np.unique(test_day_of)
                if (ic := spearman(
                    predicted[test_day_of == day], test_y[test_day_of == day],
                )) is not None
            ]
            if daily:
                per_fold[label_of(config)].append(float(np.mean(daily)))
    return SearchResult(
        mean_ic={name: float(np.mean(ics)) for name, ics in per_fold.items() if ics},
        per_fold=dict(per_fold),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bars")
    parser.add_argument("out", help="JSONL: one record per pass, resumable")
    parser.add_argument("--null", type=int, default=0)
    parser.add_argument("--horizon", type=int, action="append")
    parser.add_argument("--label", choices=("forward", "idio"), default="forward")
    args = parser.parse_args()

    by_ticker = load_bars(pathlib.Path(args.bars))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple[int, int]] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("label") == args.label:
                    done.add((record["horizon"], record["rep"]))

    horizons = tuple(args.horizon) if args.horizon else HORIZONS
    with out.open("a", encoding="utf-8") as handle:
        for horizon in horizons:
            panel = build_panel(by_ticker, horizon, label=args.label)
            fold_list = folds(len(panel.days), horizon)
            print(f"\n== horizon {horizon} ({args.label}): rows={len(panel.y)} "
                  f"tickers={len(panel.tickers)} days={len(panel.days)} "
                  f"folds={len(fold_list)} purge={horizon - 1}", flush=True)
            for rep in range(-1, args.null):
                if (horizon, rep) in done:
                    continue
                result = run_search(
                    panel, horizon, shuffle=rep >= 0,
                    rng=np.random.default_rng(SEED + 1_000 * horizon + rep),
                )
                if not result.mean_ic:
                    continue
                record = {
                    "label": args.label, "horizon": horizon, "rep": rep,
                    "best": result.best_ic, "best_config": result.best,
                    "positive_fold_share": result.positive_fold_share(result.best),
                    "folds": len(result.per_fold[result.best]),
                    "mean_ic": result.mean_ic,
                }
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                if rep < 0:
                    print(f"   REAL best={result.best_ic:+.4f} ({result.best}) "
                          f"positive folds="
                          f"{result.positive_fold_share(result.best) * 100:.0f}%",
                          flush=True)
                elif (rep + 1) % 10 == 0:
                    print(f"   null {rep + 1}/{args.null}: best={result.best_ic:+.4f}",
                          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
