"""Read-only news research inventory/export/evaluation. Never modifies live policy."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine
from webapp.board.db import normalize_database_url
from webapp.news_research import adapter, experiment, report


def guarded(path: Path) -> Path:
    resolved = path.resolve()
    if any(part.casefold().startswith("study_g") for part in resolved.parts):
        raise ValueError("reserved forward evidence must remain sealed")
    return resolved


def read_dataset(path: Path) -> experiment.Dataset:
    raw = json.loads(guarded(path).read_text())
    return experiment.Dataset(
        opportunities=tuple(
            experiment.Opportunity(
                key=r["key"], ticker=r["ticker"], day=date.fromisoformat(r["day"]),
                at=datetime.fromisoformat(r["at"]), rs=r["rs"], news=r["news"],
                base=r.get("base", True), missing=tuple(r.get("missing", ())),
            ) for r in raw["opportunities"]
        ),
        prices=tuple(
            experiment.Price(
                ticker=r["ticker"], at=datetime.fromisoformat(r["at"]),
                known_at=datetime.fromisoformat(r["known_at"]), value=r["value"],
            ) for r in raw["prices"]
        ),
        sessions=tuple(date.fromisoformat(day) for day in raw["sessions"]),
        identity=raw["identity"], as_of=datetime.fromisoformat(raw["as_of"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "export", "evaluate"))
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--asof", type=datetime.fromisoformat)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = guarded(args.output)
    if output.exists():
        parser.error("refusing to overwrite existing evidence")
    now = args.asof or datetime.now(UTC)
    if now.tzinfo is None:
        parser.error("--asof requires a timezone")
    if args.since is not None and (args.since.tzinfo is None or args.since > now):
        parser.error("--since must be aware and not later than --asof")
    if args.operation == "evaluate" and args.input:
        result = asdict(experiment.evaluate(read_dataset(args.input)))
    else:
        if args.sqlite:
            source = guarded(args.sqlite)
            if not source.is_file():
                parser.error("SQLite source must already exist")
            url = f"sqlite:///file:{quote(str(source))}?mode=ro&uri=true"
        else:
            url = os.getenv("DATABASE_URL")
            if not url:
                parser.error("provide --sqlite or a securely configured DATABASE_URL")
        engine = create_engine(normalize_database_url(url))
        try:
            if args.operation == "inspect":
                result = asdict(report.build(engine, now))
            else:
                data = adapter.export(engine, now, since=args.since)
                result = asdict(data if args.operation == "export" else experiment.evaluate(data))
        finally:
            engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str, allow_nan=False) + "\n")
    print(json.dumps({"operation": args.operation, "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
