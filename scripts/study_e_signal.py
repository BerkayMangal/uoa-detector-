"""Study E — does anything in our daily closes predict the next session?

    uv run --group research python scripts/study_e_signal.py <closes.csv> [--null N]

Protocol is fixed by ``docs/study-E-signal-preregistration.md`` and this script
implements it literally. Nothing here may be tuned after a result is seen (D4).

    features   close-only, computed from information available at the close of t
    label      next-session log return, scored as a within-day rank IC
    folds      expanding walk-forward, train 120 / test 21, rolled by 21
    search     a fixed grid of 64 XGBoost configurations per fold
    null       the SAME search on labels permuted WITHIN each day, N times

The null is the point. A 64-config search over six folds will find something on
253 days of ten correlated names whether or not anything is there; the only way
to know how much of the result is the search itself is to run the search against
shuffled labels and read the real number against that distribution.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

TRAIN = 120          # sessions in the first training window
TEST = 21            # sessions per out-of-sample fold (about one month)
STEP = 21            # roll
HORIZONS = (1, 5)    # the horizons the outcome job already scores
MARKET = "SPY"

GRID = [
    {"max_depth": d, "learning_rate": lr, "subsample": ss,
     "min_child_weight": mcw, "colsample_bytree": cs}
    for d, lr, ss, mcw, cs in product(
        (2, 3, 4, 6), (0.03, 0.1), (0.7, 1.0), (1, 5), (0.7, 1.0),
    )
]
assert len(GRID) == 64, len(GRID)

FEATURES = [
    "r_1", "r_5", "r_20", "vol_20", "z_20",
    "dist_high_50", "dist_low_50", "mkt_r_1", "mkt_r_5", "rel_r_5",
]


def load_closes(path: Path) -> pd.DataFrame:
    """(day x ticker) closes from the exported CSV, oldest first."""
    raw = pd.read_csv(path, names=["ticker", "day", "close"], parse_dates=["day"])
    wide = raw.pivot(index="day", columns="ticker", values="close").sort_index()
    if wide.isna().to_numpy().sum():
        msg = "closes have gaps; the study refuses to interpolate prices"
        raise SystemExit(msg)
    return wide


def build_panel(wide: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Long panel of features at t and the label realised over t+1..t+horizon."""
    logp = np.log(wide)
    r1 = logp.diff()
    mkt = r1[MARKET]
    frames = []
    for ticker in wide.columns:
        close = wide[ticker]
        lr = r1[ticker]
        feat = pd.DataFrame({
            "r_1": lr,
            "r_5": logp[ticker].diff(5),
            "r_20": logp[ticker].diff(20),
            "vol_20": lr.rolling(20).std(),
            "z_20": (close - close.rolling(20).mean()) / close.rolling(20).std(),
            "dist_high_50": close / close.rolling(50).max() - 1.0,
            "dist_low_50": close / close.rolling(50).min() - 1.0,
            "mkt_r_1": mkt,
            "mkt_r_5": logp[MARKET].diff(5),
        })
        feat["rel_r_5"] = feat["r_5"] - feat["mkt_r_5"]
        # The label is strictly forward: log return from t to t+horizon.
        feat["y"] = logp[ticker].shift(-horizon) - logp[ticker]
        feat["ticker"] = ticker
        feat["day"] = wide.index
        frames.append(feat)
    panel = pd.concat(frames, ignore_index=True).dropna()
    return panel.sort_values(["day", "ticker"]).reset_index(drop=True)


def rank_ic(frame: pd.DataFrame) -> float:
    """Mean within-day Spearman correlation of prediction and realised return."""
    per_day = []
    for _day, group in frame.groupby("day"):
        if len(group) < 3:
            continue
        pred = group["pred"].rank()
        real = group["y"].rank()
        if pred.std() == 0 or real.std() == 0:
            continue
        per_day.append(float(np.corrcoef(pred, real)[0, 1]))
    return float(np.mean(per_day)) if per_day else float("nan")


@dataclass
class FoldResult:
    fold: int
    train_days: int
    test_days: int
    ic: float
    config: dict[str, float] = field(default_factory=dict)


def folds(days: list[pd.Timestamp]) -> list[tuple[list[pd.Timestamp], list[pd.Timestamp]]]:
    out = []
    start = TRAIN
    while start + TEST <= len(days):
        out.append((days[:start], days[start:start + TEST]))
        start += STEP
    return out


def run_once(panel: pd.DataFrame, *, shuffle: bool, rng: np.random.Generator) -> list[FoldResult]:
    """One full pass of the pre-registered search. ``shuffle`` permutes y within each day."""
    from xgboost import XGBRegressor

    work = panel.copy()
    if shuffle:
        work["y"] = work.groupby("day")["y"].transform(
            lambda s: rng.permutation(s.to_numpy()),
        )

    days = sorted(work["day"].unique())
    results: list[FoldResult] = []
    for index, (train_days, test_days) in enumerate(folds(days)):
        tr = work[work["day"].isin(train_days)]
        te = work[work["day"].isin(test_days)]
        # Configuration is chosen on the LAST 21 training days only, never on test.
        inner_cut = train_days[-TEST:]
        inner_tr = tr[~tr["day"].isin(inner_cut)]
        inner_va = tr[tr["day"].isin(inner_cut)]
        best_ic, best_cfg = -np.inf, GRID[0]
        for cfg in GRID:
            model = XGBRegressor(
                n_estimators=120, tree_method="hist", verbosity=0,
                random_state=0, n_jobs=1, **cfg,
            )
            model.fit(inner_tr[FEATURES], inner_tr["y"])
            scored = inner_va.assign(pred=model.predict(inner_va[FEATURES]))
            ic = rank_ic(scored)
            if np.isfinite(ic) and ic > best_ic:
                best_ic, best_cfg = ic, cfg
        final = XGBRegressor(
            n_estimators=120, tree_method="hist", verbosity=0,
            random_state=0, n_jobs=1, **best_cfg,
        )
        final.fit(tr[FEATURES], tr["y"])
        scored = te.assign(pred=final.predict(te[FEATURES]))
        results.append(FoldResult(
            fold=index, train_days=len(train_days), test_days=len(test_days),
            ic=rank_ic(scored), config=best_cfg,
        ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("closes")
    parser.add_argument("--null", type=int, default=0, help="shuffled-label repetitions")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    wide = load_closes(Path(args.closes))
    print(f"sessions={len(wide)} tickers={wide.shape[1]} "
          f"span={wide.index.min().date()}..{wide.index.max().date()}")

    report: dict[str, object] = {"sessions": len(wide), "tickers": list(wide.columns)}
    for horizon in HORIZONS:
        panel = build_panel(wide, horizon)
        days = sorted(panel["day"].unique())
        fold_list = folds(days)
        print(f"\n== horizon {horizon} session(s): rows={len(panel)} "
              f"days={len(days)} folds={len(fold_list)} fits={len(GRID) * len(fold_list)}")
        real = run_once(panel, shuffle=False, rng=np.random.default_rng(0))
        for r in real:
            print(f"   fold {r.fold}: train={r.train_days} test={r.test_days} IC={r.ic:+.4f}")
        mean_ic = float(np.nanmean([r.ic for r in real]))
        positive = sum(1 for r in real if r.ic > 0)
        print(f"   mean OOS rank IC = {mean_ic:+.4f}   positive folds = {positive}/{len(real)}")

        entry: dict[str, object] = {
            "mean_ic": mean_ic, "positive_folds": positive, "folds": len(real),
            "per_fold": [{"fold": r.fold, "ic": r.ic, "config": r.config} for r in real],
        }

        if args.null:
            print(f"   running the null: {args.null} shuffled-label passes "
                  f"({args.null * len(GRID) * len(fold_list)} fits)", flush=True)
            null_means = []
            for rep in range(args.null):
                rng = np.random.default_rng(1000 + rep)
                null_means.append(float(np.nanmean(
                    [r.ic for r in run_once(panel, shuffle=True, rng=rng)],
                )))
                if (rep + 1) % 10 == 0:
                    arr = np.array(null_means)
                    print(f"      {rep + 1}/{args.null} null mean={arr.mean():+.4f} "
                          f"p99={np.percentile(arr, 99):+.4f}", flush=True)
            arr = np.array(null_means)
            p99 = float(np.percentile(arr, 99))
            beats = bool(mean_ic > p99)
            pct = float((arr < mean_ic).mean() * 100)
            print(f"   NULL: mean={arr.mean():+.4f} sd={arr.std():.4f} "
                  f"p95={np.percentile(arr, 95):+.4f} p99={p99:+.4f}")
            print(f"   the real result sits at the {pct:.1f}th percentile of the null")
            print(f"   threshold 1 (beats p99 of its own null): {'PASS' if beats else 'FAIL'}")
            entry |= {"null_mean": float(arr.mean()), "null_sd": float(arr.std()),
                      "null_p99": p99, "real_percentile": pct, "beats_null_p99": beats}
        report[f"horizon_{horizon}"] = entry

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
