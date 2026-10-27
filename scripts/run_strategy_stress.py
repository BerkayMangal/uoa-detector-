"""Offline stress audit; an existing result is never overwritten.

uv run python scripts/run_strategy_stress.py /tmp/stress-v1/report.json
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
from webapp.robustness.research import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    records_path = args.output.with_name("replays.jsonl.gz")
    if args.output.exists() or records_path.exists():
        parser.error("refusing to overwrite frozen stress results")
    body, records = run(Path("data/study_f/bars.csv"))
    raw = "".join(
        json.dumps(r, sort_keys=True, default=str, separators=(",", ":"), allow_nan=False) + "\n"
        for r in records
    ).encode()
    compressed = gzip.compress(raw, mtime=0)
    body["replay_artifact"] = {
        "name": records_path.name,
        "rows": len(records),
        "sha256": hashlib.sha256(compressed).hexdigest(),
    }
    body["report_hash"] = digest(body)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents accidental concurrent replacement.
    with records_path.open("xb") as handle:
        handle.write(compressed)
    with args.output.open("x") as handle:
        handle.write(
            json.dumps(body, sort_keys=True, default=str, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
    print(json.dumps({k: body[k] for k in ("portfolio_evaluations", "report_hash")}))


if __name__ == "__main__":
    main()
