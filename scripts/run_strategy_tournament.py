"""Run the frozen grid once; no HTTP optimizer or overwrite of old results.

uv run python scripts/run_strategy_tournament.py /tmp/tournament-report.json
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp.alpha_lab.db import digest
from webapp.tournament.research import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite a frozen result")
    grid_path = args.output.with_name("grid.jsonl.gz")
    if grid_path.exists():
        parser.error("refusing to overwrite frozen grid")
    body = run(Path("data/study_f/bars.csv"), Path("data/study_e/flow_daily.csv"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = body.pop("grid")
    body.pop("report_hash")
    encoded = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        for row in records
    ).encode()
    compressed = gzip.compress(encoded, mtime=0)
    grid_path.write_bytes(compressed)
    body["grid_artifact"] = {
        "name": grid_path.name,
        "rows": len(records),
        "sha256": hashlib.sha256(compressed).hexdigest(),
    }
    body["report_hash"] = digest(body)
    args.output.write_text(
        json.dumps(body, sort_keys=True, default=str, separators=(",", ":"), allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                k: body[k]
                for k in (
                    "configuration_trials",
                    "distinct_portfolio_evaluations",
                    "eligible",
                    "overall_id",
                    "report_hash",
                )
            }
        ),
        flush=True,
    )
    for w in body["winners"]:
        print(
            w["config"]["family"],
            w["id"],
            "PAPER",
            w["paper_enabled"],
            "test",
            w["test"]["metrics"],
            "joint_p",
            w["bootstrap_p"],
        )


if __name__ == "__main__":
    main()
