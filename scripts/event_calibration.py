"""Offline event workbench. Use python -m scripts.event_calibration --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine
from webapp.event_calibration.archive import export
from webapp.event_calibration.inventory import inventory
from webapp.event_calibration.model import digest, load
from webapp.event_calibration.probe import probe
from webapp.event_calibration.research import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inventory", "export", "probe", "calibrate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--ticker", default="NFLX")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--days", nargs=2, type=date.fromisoformat)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite evidence")
    code = 0
    if args.operation == "inventory":
        result = inventory()
    elif args.operation == "probe":
        if not args.days:
            parser.error("--days requires two completed sessions")
        result = asyncio.run(probe(args.ticker, tuple(args.days)))
        code = 0 if result["requests"] else 2
    elif args.operation == "export":
        if args.sqlite:
            if not args.sqlite.is_file():
                parser.error("SQLite source must already exist; no schema is created")
            url = f"sqlite:///file:{quote(str(args.sqlite.resolve()))}?mode=ro&uri=true"
        else:
            url = os.getenv("DATABASE_URL")
            if not url:
                parser.error("provide --sqlite or a securely bound DATABASE_URL")
        engine = create_engine(url)
        try:
            result = export(
                engine,
                datetime.now(UTC),
                since=args.since,
                tickers=tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip()),
            ).model_dump(mode="json")
        finally:
            engine.dispose()
    else:
        if args.input is None:
            parser.error("--input is required")
        result = run(load(args.input))
        code = 0 if result["status"] == "EXPLORATORY_CANDIDATE_ONLY" else 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result["report_hash"] = digest(result)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False, default=str, allow_nan=False)
        handle.write("\n")
    print(
        json.dumps(
            {
                "status": result.get("status", "EXPORTED"),
                "output": str(args.output),
                "report_hash": result["report_hash"],
            }
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
