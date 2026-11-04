"""Offline frozen-roster sensitivity; no network, new selection or live promotion."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from webapp.history_capture.capture import digest
from webapp.horizon_research.engine import run
from webapp.horizon_research.model import ROOT
from webapp.horizon_research.source import load


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(part.casefold().startswith("study_g") for part in output.parts):
        parser.error("reserved evidence path")
    if output.exists():
        parser.error("refusing to overwrite research evidence")
    result = run(load(args.source))
    units = result.pop("units")
    encoded = json.dumps(units, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    compressed = gzip.compress(encoded, mtime=0)
    result.update(
        ledger_file="units.json.gz",
        ledger_sha256=hashlib.sha256(compressed).hexdigest(),
        ledger_content_hash=digest(units),
        cli_sha256=hashlib.sha256((ROOT / "scripts/horizon_research.py").read_bytes()).hexdigest(),
        winner=None,
        validated_alpha=False,
    )
    result["report_hash"] = digest(result)
    output.mkdir(parents=True, exist_ok=False)
    (output / "units.json.gz").write_bytes(compressed)
    (output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report_hash": result["report_hash"], "rows": len(result["rows"]), "units": len(units)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
