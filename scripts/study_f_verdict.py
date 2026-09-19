"""Read Study F's passes and apply §7's four thresholds. No judgement, no prose.

    uv run --group research python scripts/study_f_verdict.py \\
        out/study_f_forward.jsonl --idio out/study_f_idio.jsonl

Input is the JSONL ``study_f_runner.py`` appends: one record per pass, ``rep=-1``
for the real arm and ``rep>=0`` for the shuffled-label null. This script decides
nothing that was not decided in ``docs/study-F-preregistration.md`` §7 before any
number existed:

    T1  best-of-ten mean OOS rank IC > the 99th percentile of the null's
        best-of-ten distribution
    T2  the winning configuration's IC positive in >= 75% of folds
    T3  T1 again on the market-neutral label
    T4  evaluated only if T1-T3 pass

A threshold that is not evaluated is printed as NOT EVALUATED with the reason.
Writing "n/a" where a gate was never reached, or quietly dropping it, is how a
study ends up claiming more than it tested.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass

import numpy as np

T1_PERCENTILE = 99.0
T2_POSITIVE_FOLD_SHARE = 0.75
NULL_PASSES = 200


@dataclass(frozen=True)
class Arm:
    """One label's passes at one horizon."""

    horizon: int
    label: str
    real_best: float
    real_config: str
    real_positive_share: float
    real_folds: int
    null_bests: np.ndarray

    @property
    def null_p99(self) -> float:
        return float(np.percentile(self.null_bests, T1_PERCENTILE))

    @property
    def real_percentile(self) -> float:
        return float((self.null_bests < self.real_best).mean() * 100.0)

    @property
    def t1(self) -> bool:
        return bool(self.real_best > self.null_p99)

    @property
    def t2(self) -> bool:
        return self.real_positive_share >= T2_POSITIVE_FOLD_SHARE


def read_arm(path: pathlib.Path, horizon: int, label: str) -> Arm | None:
    real: dict[str, object] | None = None
    nulls: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("label", "forward") != label or record["horizon"] != horizon:
            continue
        if record["rep"] < 0:
            real = record
        else:
            nulls.append(float(record["best"]))
    if real is None or not nulls:
        return None
    return Arm(
        horizon=horizon, label=label, real_best=float(real["best"]),
        real_config=str(real["best_config"]),
        real_positive_share=float(real["positive_fold_share"]),
        real_folds=int(real["folds"]), null_bests=np.asarray(nulls, dtype=float),
    )


def report(arm: Arm, idio: Arm | None) -> bool:
    """Print one horizon's four gates. Returns True only if the horizon passed."""
    print(f"\n=== horizon {arm.horizon} ===")
    print(f"  real best-of-ten mean OOS rank IC : {arm.real_best:+.4f} "
          f"({arm.real_config})")
    print(f"  null passes                       : {len(arm.null_bests)}"
          f"{'' if len(arm.null_bests) >= NULL_PASSES else f' — SHORT of {NULL_PASSES}'}")
    print(f"  null best-of-ten mean / sd        : {arm.null_bests.mean():+.4f} / "
          f"{arm.null_bests.std():.4f}")
    print(f"  null p95 / p99                    : "
          f"{np.percentile(arm.null_bests, 95):+.4f} / {arm.null_p99:+.4f}")
    print(f"  the real result sits at the {arm.real_percentile:.1f}th percentile "
          f"of its own null")

    print(f"  T1 beats its own null's p99       : {'PASS' if arm.t1 else 'FAIL'}")
    print(f"  T2 positive in >= 75% of folds    : "
          f"{'PASS' if arm.t2 else 'FAIL'} "
          f"({arm.real_positive_share * 100:.0f}% of {arm.real_folds})")

    if not arm.t1:
        print("  T3 market-neutral label           : NOT EVALUATED — T1 failed")
        print("  T4 survives costs                 : NOT EVALUATED — T1 failed")
        return False
    if idio is None:
        print("  T3 market-neutral label           : NOT EVALUATED — no idio passes "
              "in the input")
        print("  T4 survives costs                 : NOT EVALUATED — T3 not evaluated")
        return False
    print(f"  T3 market-neutral label           : {'PASS' if idio.t1 else 'FAIL'} "
          f"(IC {idio.real_best:+.4f} vs null p99 {idio.null_p99:+.4f})")
    if not (arm.t2 and idio.t1):
        print("  T4 survives costs                 : NOT EVALUATED — T1-T3 did not all pass")
        return False
    print("  T4 survives costs                 : REQUIRED — T1-T3 passed, so the "
          "long-short decile book must be priced through the cost model "
          "(slippage_pct 0.02 + $0.65/leg) before any claim is made")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("forward", help="JSONL from the forward-label run")
    parser.add_argument("--idio", default="", help="JSONL from the --label idio run")
    parser.add_argument("--horizon", type=int, action="append")
    args = parser.parse_args()

    forward_path = pathlib.Path(args.forward)
    idio_path = pathlib.Path(args.idio) if args.idio else None
    horizons = tuple(args.horizon) if args.horizon else (1, 5)

    any_pass = False
    for horizon in horizons:
        arm = read_arm(forward_path, horizon, "forward")
        if arm is None:
            print(f"\n=== horizon {horizon} === no complete pass set in "
                  f"{forward_path}", file=sys.stderr)
            continue
        idio = read_arm(idio_path, horizon, "idio") if idio_path else None
        any_pass |= report(arm, idio)

    print("\nVERDICT: " + (
        "at least one horizon cleared T1-T3; T4 decides whether it is money"
        if any_pass else
        "REJECTED — no horizon cleared its own null. Per §7 nothing is retuned."
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
