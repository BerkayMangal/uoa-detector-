"""Run the options PAPER tracker once, locally, against real Unusual Whales data.

    set -a && . ./.env && set +a
    uv run python scripts/options_alpha_tracker_e2e.py --db /tmp/tracker.db \
        --out artifacts/options-alpha-v1/tracker_e2e.json

This is the SAME code the live board refresher runs as its ``options_paper`` daily
job (``webapp/board/options_paper.py``); only the database is a local SQLite file
instead of production Postgres. It exists so the chain

    data -> hypothesis -> contract/spread -> cost/risk -> PAPER card -> tracking -> outcome

can be shown end to end on real data before the job has run in production.

Spend: one ``ohlc/1d`` call for SPY (the session calendar), then one ``/historic``
call per leg of each open card, each behind the atomic quota ledger. Never prints
the key. Writes one summary artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from webapp.board.daily_close import run_daily_close_job
from webapp.board.db import make_engine
from webapp.board.options_paper import (
    AlfaOptPaperMark,
    AlfaOptPaperOutcome,
    AlfaOptPaperPosition,
    read_tracker,
    run_options_paper,
)
from webapp.board.quota_ledger import reserved_on
from webapp.board.settings import load_board_settings

from uoa_detector.calibration import load_profile
from uoa_detector.config import Credentials
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    now = datetime.now(UTC)
    engine = make_engine(f"sqlite:///{args.db}")
    settings = load_board_settings()
    profile = load_profile(pathlib.Path("profiles/v5_default.yaml"))
    client = UnusualWhalesClient(
        api_key=Credentials().require_unusual_whales_api_key(),
        settings=profile.data_sources.unusual_whales,
    )
    try:
        closes = await run_daily_close_job(client, engine, [], now=now)
        report = await run_options_paper(client=client, engine=engine, settings=settings, now=now)
    finally:
        await client.aclose()

    view = read_tracker(engine)
    with Session(engine) as session:
        positions = {p.position_id: p for p in session.scalars(select(AlfaOptPaperPosition))}
        outcomes = {o.position_id: o for o in session.scalars(select(AlfaOptPaperOutcome))}
        marks: dict[str, list[dict[str, str]]] = {}
        for m in session.scalars(select(AlfaOptPaperMark).order_by(AlfaOptPaperMark.session)):
            marks.setdefault(m.position_id, []).append({
                "session": m.session.isoformat(), "closable_value": m.closable_value,
                "quotes": json.loads(m.quotes_json),
            })

    summary = {
        "run_at": now.isoformat(),
        "database": "local sqlite (same code as the live options_paper daily job)",
        "calendar": [r.ticker for r in closes.tickers],
        "report": report.detail(),
        "status": report.status,
        "requests_reserved_today_local_ledger": reserved_on(engine, now.date()),
        "vendor_daily_count_after": client.last_daily_request_count,
        "positions": [
            {
                "position_id": pid,
                "source": p.source_path,
                "data_origin": p.data_origin,
                "hypothesis_id": p.hypothesis_id,
                "research_status": p.research_status,
                "underlying": p.underlying,
                "structure": p.structure,
                "entry_session": p.entry_session.isoformat(),
                "marks": marks.get(pid, []),
                "outcome": None if pid not in outcomes else {
                    "plan_variant": outcomes[pid].plan_variant,
                    "reason": outcomes[pid].reason,
                    "exit_day": None if outcomes[pid].exit_day is None else outcomes[pid].exit_day.isoformat(),
                    "exit_value": outcomes[pid].exit_value,
                    "pnl_usd": outcomes[pid].pnl_usd,
                    "realised": outcomes[pid].realised,
                    "variants": json.loads(outcomes[pid].variants_json),
                },
            }
            for pid, p in sorted(positions.items(), key=lambda kv: (kv[1].entry_session, kv[0]))
        ],
        "tracker_view": {"runs": view.runs, "last_status": view.last_status},
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("report", "status", "requests_reserved_today_local_ledger",
                                              "vendor_daily_count_after")}, indent=1))
    for p in summary["positions"]:
        o = p["outcome"]
        print(f"{p['entry_session']} {p['underlying']:4s} {p['structure']:16s} {p['data_origin']:7s} "
              f"marks={len(p['marks'])} "
              + ("ACIK" if o is None else f"{o['reason']} {o['exit_day']} pnl={o['pnl_usd']}"))
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
