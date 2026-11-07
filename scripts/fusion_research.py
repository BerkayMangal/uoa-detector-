"""Seal the fixed plan, then run the bounded exploratory reused-history search."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp.fusion_research import report, search


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/study_f/bars.csv"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/fusion-research-v1"))
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if any("study_g" in p.casefold().replace("-", "_") for p in args.output.absolute().parts):
        parser.error("reserved evidence path")
    args.output.mkdir(parents=True, exist_ok=True)
    if any((args.output / name).exists() for name in ("plan.json", "report.json", "grid.jsonl.gz")):
        parser.error("refusing to overwrite a registered run")
    plan = search.plan(args.source)
    with (args.output / "plan.json").open("x") as f:
        f.write(json.dumps(plan, sort_keys=True, default=str, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(
        json.dumps(
            {
                "state": "PLAN_SEALED",
                "plan_hash": plan["plan_hash"],
                "configuration_trials": plan["candidate_count"],
                "maximum_run_seconds": plan["maximum_run_seconds"],
            }
        ),
        flush=True,
    )
    if args.plan_only:
        return 0
    progress_path = args.output / "progress.json"

    def progress(value: dict[str, object]) -> None:
        progress_path.write_text(json.dumps(value, sort_keys=True) + "\n")
        print(json.dumps(value), flush=True)

    result = search.run(args.source, plan, progress=progress)
    report.write(args.output, result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "configuration_trials",
                    "reference_replay_evaluations",
                    "cached_rosters",
                    "winner",
                    "inference",
                    "elapsed_seconds",
                    "report_hash",
                )
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
