"""Run the frozen exploratory search, never from an HTTP request.

uv run python scripts/calibrate_desk.py data/study_f/bars.csv artifacts/calibration-v1/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.calibration.model import load_csv
from webapp.calibration.search import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() != Path("data/study_f/bars.csv").resolve():
        parser.error("v1 is frozen to reused Study F; no Study G or future prices")
    if args.output.exists():
        parser.error("report already exists; refuse to overwrite a frozen search")
    report = run(load_csv(args.source), args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, default=str, sort_keys=True, indent=2, allow_nan=False)+"\n")
    print(json.dumps({key: report[key] for key in ("verdict", "configuration_trials", "portfolio_evaluations", "train_eligible", "validation_eligible", "report_sha256")}))


if __name__ == "__main__":
    main()
