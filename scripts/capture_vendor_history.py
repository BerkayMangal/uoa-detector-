"""Capture the frozen historical window privately; no research return computation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ruamel.yaml import YAML
from webapp.history_capture.capture import ROOT
from webapp.history_capture.capture_v2 import capture, settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = settings()
    floor = YAML(typ="safe").load((ROOT / "profiles/history_access_v1.yaml").read_text())[
        "measured_earliest_flow"
    ]
    cfg = cfg.model_copy(update={"start": max(cfg.start, floor)})
    result = asyncio.run(capture(args.output, cfg=cfg))
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("status", "reason", "calls_this_run", "remaining", "sealed", "resumed")
            }
        )
    )
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
