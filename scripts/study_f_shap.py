"""Study F §6's SHAP pass, run as a measurement rather than a picture.

    uv run --group research python scripts/study_f_shap.py \\
        data/study_f/bars.csv out/shap.json --shuffles 20

§6 permits SHAP on the winning configuration **for description only**: it cannot
change the verdict, and no feature may be added, removed or re-weighted on the
strength of a plot. The result document went further and said a SHAP plot of a failed
search describes nothing but noise — which was an argument, not a number.

This turns it into a number. The winning configuration is fitted exactly as the
search fitted it (same purged folds, same training-window standardisation), and its
mean |SHAP| per feature is taken on each test fold. Then the identical thing is done
on labels shuffled within each day, N times. Two distributions come out:

  real-vs-shuffled    how much the real arm's feature ranking agrees with a
                      shuffled arm's
  shuffled-vs-shuffled how much two shuffled arms agree with each other

If the first distribution sits inside the second, the real arm's "important features"
are indistinguishable from what the search invents on destroyed labels, and any SHAP
plot of this model is a plot of the search rather than of the data. That is the claim
§2 makes about the whole study, restated at the level of the explanation a reader
would actually be shown.

Nothing here can change the verdict, and nothing here is used to select a feature.
It exists so that "SHAP would have explained noise convincingly" stops being
something the document asserts and becomes something it measured.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import pathlib
import sys
from typing import Any

import numpy as np

_RUNNER = pathlib.Path(__file__).resolve().parent / "study_f_runner.py"


def _runner() -> Any:
    """The search itself, imported rather than reimplemented.

    Reusing ``build_panel``, ``folds`` and ``standardise`` is the point: a SHAP pass
    fitted on a differently-built panel would describe a model the study never ran.
    """
    spec = importlib.util.spec_from_file_location("study_f_runner", _RUNNER)
    if spec is None or spec.loader is None:
        msg = f"cannot import the runner at {_RUNNER}"
        raise SystemExit(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _xgb_kwargs(label: str) -> dict[str, object]:
    """Parse ``xgb_d3_n100_lr0.03`` back into the configuration the search used."""
    if not label.startswith("xgb_"):
        msg = f"SHAP is defined on the tree configurations; got {label!r}"
        raise SystemExit(msg)
    parts = label.split("_")
    return {
        "max_depth": int(parts[1][1:]),
        "n_estimators": int(parts[2][1:]),
        "learning_rate": float(parts[3][2:]),
    }


def feature_importance(
    runner: Any, panel: Any, horizon: int, config: dict[str, object],
    y: np.ndarray, seed: int,
) -> np.ndarray | None:
    """Mean |SHAP| per feature, averaged over the walk-forward's test folds.

    Fitted fold by fold on the training window, explained on the test window — the
    same split the IC was measured on. A feature's number is therefore "how much this
    model leaned on it when predicting days it had not seen".
    """
    import shap
    from xgboost import XGBRegressor

    per_fold: list[np.ndarray] = []
    for train_days, test_days in runner.folds(len(panel.days), horizon):
        in_train = np.isin(panel.day_of, np.fromiter(train_days, dtype=int))
        in_test = np.isin(panel.day_of, np.fromiter(test_days, dtype=int))
        if int(in_train.sum()) < 50 or int(in_test.sum()) < 10:
            continue
        train_x, test_x = runner.standardise(panel.x[in_train], panel.x[in_test])
        model = XGBRegressor(
            subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
            random_state=seed, n_jobs=1, verbosity=0, tree_method="hist",
            **config,
        )
        model.fit(train_x, y[in_train])
        values = shap.TreeExplainer(model).shap_values(test_x)
        per_fold.append(np.abs(np.asarray(values, dtype=float)).mean(axis=0))
    if not per_fold:
        return None
    return np.mean(per_fold, axis=0)


def _spearman(a: np.ndarray, b: np.ndarray, ranks: Any) -> float:
    ra, rb = ranks(a), ranks(b)
    va, vb = ra - ra.mean(), rb - rb.mean()
    denom = math.sqrt(float((va**2).sum()) * float((vb**2).sum()))
    return float("nan") if denom <= 0.0 else float((va * vb).sum() / denom)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bars")
    parser.add_argument("out")
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--label", choices=("forward", "idio"), default="forward")
    parser.add_argument("--config", default="xgb_d3_n100_lr0.03")
    parser.add_argument("--shuffles", type=int, default=20)
    args = parser.parse_args()

    runner = _runner()
    config = _xgb_kwargs(args.config)
    bars = runner.load_bars(pathlib.Path(args.bars))
    panel = runner.build_panel(bars, args.horizon, label=args.label)
    names = list(runner.PANEL_FEATURES)
    print(f"panel: {len(panel.y)} rows, {len(panel.tickers)} tickers, "
          f"{len(panel.days)} days, config {args.config}", flush=True)

    real = feature_importance(
        runner, panel, args.horizon, config, panel.y, runner.SEED,
    )
    if real is None:
        print("no usable fold", file=sys.stderr)
        return 1
    order = np.argsort(-real)
    print("real arm, top 8 by mean |SHAP|:", flush=True)
    for i in order[:8]:
        print(f"    {names[i]:<22} {real[i]:.6f}", flush=True)

    shuffled: list[np.ndarray] = []
    for rep in range(args.shuffles):
        rng = np.random.default_rng(runner.SEED + 5_000 + rep)
        y = runner.shuffle_within_day(panel.y, panel.day_of, rng)
        importance = feature_importance(
            runner, panel, args.horizon, config, y, runner.SEED,
        )
        if importance is not None:
            shuffled.append(importance)
        if (rep + 1) % 5 == 0:
            print(f"  shuffled {rep + 1}/{args.shuffles}", flush=True)

    ranks = runner._ranks
    real_vs = [_spearman(real, s, ranks) for s in shuffled]
    between = [
        _spearman(shuffled[i], shuffled[j], ranks)
        for i in range(len(shuffled))
        for j in range(i + 1, len(shuffled))
    ]

    # Top-5 overlap, which is what a reader of a SHAP plot actually takes away.
    real_top5 = {names[i] for i in order[:5]}
    overlaps = [
        len(real_top5 & {names[i] for i in np.argsort(-s)[:5]}) for s in shuffled
    ]
    between_overlaps = [
        len({names[i] for i in np.argsort(-shuffled[a])[:5]}
            & {names[i] for i in np.argsort(-shuffled[b])[:5]})
        for a in range(len(shuffled))
        for b in range(a + 1, len(shuffled))
    ]

    result = {
        "horizon": args.horizon, "label": args.label, "config": args.config,
        "shuffles": len(shuffled),
        "features": names,
        "real_importance": real.tolist(),
        "real_top5": sorted(real_top5),
        "real_vs_shuffled_spearman": {
            "mean": float(np.mean(real_vs)), "sd": float(np.std(real_vs)),
            "min": float(np.min(real_vs)), "max": float(np.max(real_vs)),
        },
        "shuffled_vs_shuffled_spearman": {
            "mean": float(np.mean(between)), "sd": float(np.std(between)),
            "min": float(np.min(between)), "max": float(np.max(between)),
        },
        "real_top5_overlap_with_shuffled": {
            "mean": float(np.mean(overlaps)), "max": int(np.max(overlaps)),
        },
        "shuffled_top5_overlap_between": {
            "mean": float(np.mean(between_overlaps)),
            "max": int(np.max(between_overlaps)),
        },
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"\nreal vs shuffled  Spearman: mean {np.mean(real_vs):+.3f} "
          f"sd {np.std(real_vs):.3f}  range [{np.min(real_vs):+.3f}, {np.max(real_vs):+.3f}]")
    print(f"shuffled vs each other:     mean {np.mean(between):+.3f} "
          f"sd {np.std(between):.3f}  range [{np.min(between):+.3f}, {np.max(between):+.3f}]")
    print(f"top-5 overlap, real vs shuffled: mean {np.mean(overlaps):.2f} of 5")
    print(f"top-5 overlap, shuffled pairs:   mean {np.mean(between_overlaps):.2f} of 5")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
