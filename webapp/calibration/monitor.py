"""Future independent PAPER records; never backfills a missed entry decision."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Table, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import classify
from webapp.alpha_lab.db import append, digest, dumps
from webapp.alpha_lab.flow import utc
from webapp.board.db import session_factory
from webapp.calibration import report, storage
from webapp.calibration.model import Config, features, horizon_days, matches
from webapp.calibration.settings import calendar, settings

STATE: dict[str, object] = {}
_RUNNING = threading.Lock()
_logger = logging.getLogger(__name__)


def observe(engine: Engine, now: datetime) -> None:
    now = utc(now)
    s, cal, frozen = settings(), calendar(), report.load()
    storage.ensure(engine)
    selected = frozen.get("selected")
    if frozen["verdict"] != "EXPERIMENTAL_FORWARD_ONLY" or not selected:
        STATE.update(status="no_candidate", at=now.isoformat(), decisions=0)
        return
    config = Config(**selected["config"])
    bars = tuple(b for b in storage.bars_at(engine, now) if b.ticker in frozen["tickers"])
    panel = {(b.ticker, b.day): b for b in bars}
    latest = max((b.day for b in bars if b.ticker == "SPY"), default=None)
    eligible_count = 0
    decisions: list[dict[str, Any]] = []  # Frozen JSON storage adapter.
    if latest is not None and latest >= s.prospective_start:
        for f in features(bars, config.lookback):
            if f.day != latest or not matches(f, config):
                continue
            eligible_count += 1
            dates = horizon_days(f.day, s.primary_sessions)
            if dates is None:
                continue
            entry, end = dates
            entry_open = datetime.combine(entry, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(UTC)
            # This is a prospective instruction, not a reconstruction after open.
            if now >= entry_open:
                continue
            key = digest((frozen["report_sha256"], f.ticker, f.day))
            payload = {"config": asdict(config), "feature": asdict(f), "entry": entry, "exit": end,
                       "report_hash": frozen["report_sha256"], "profile_hash": s.profile_hash,
                       "unit_budget_usd": s.position_usd, "scope": "independent_paper_record"}
            decisions.append(dict(key=key,ticker=f.ticker,day=f.day,observed_at=now,payload_json=dumps(payload)))
    append(engine, cast(Table, storage.Decision.__table__), decisions)
    with session_factory(engine)() as session:
        pending = list(session.scalars(select(storage.Decision).where(storage.Decision.observed_at <= now)))
        known = {r.key: json.loads(r.payload_json) for r in session.scalars(select(storage.Result).where(storage.Result.observed_at <= now))}
    for decision in pending:
        data = json.loads(decision.payload_json)
        if data.get("report_hash") != frozen["report_sha256"]:
            continue
        entry_day, exit_day = date.fromisoformat(data["entry"]), date.fromisoformat(data["exit"])
        entry_key, exit_key = digest((decision.key, "entry")), digest((decision.key, "closed"))
        if digest((decision.key, "unfilled")) in known:
            continue
        entry_record = known.get(entry_key)
        b, spy_entry = panel.get((decision.ticker, entry_day)), panel.get(("SPY", entry_day))
        slip = s.slippage_bps / 10000
        if entry_record is None and b is not None and b.valid:
            entry_open = datetime.combine(entry_day, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(UTC)
            if utc(decision.observed_at) >= entry_open:
                continue
            price = b.open * (1+slip)
            qty = int(data["unit_budget_usd"] / price)
            if qty <= 0:
                append(engine, cast(Table, storage.Result.__table__), [dict(key=digest((decision.key, "unfilled")),decision_key=decision.key,kind="unfilled",observed_at=now,payload_json=dumps({"reason":"whole share exceeds unit budget"}))])
                continue
            entry_record = {"entry": entry_day, "price": price, "quantity": qty, "spy_open": spy_entry.open if spy_entry is not None and spy_entry.valid else None}
            append(engine, cast(Table, storage.Result.__table__), [dict(key=entry_key,decision_key=decision.key,kind="entry",observed_at=now,payload_json=dumps(entry_record))])
        out, spy_out = panel.get((decision.ticker, exit_day)), panel.get(("SPY", exit_day))
        if entry_record is None or exit_key in known or out is None or not out.valid:
            continue
        qty, price = entry_record["quantity"], entry_record["price"]
        exit_price = out.close * (1-slip)
        excess = (exit_price / price - spy_out.close * (1-slip) / (entry_record["spy_open"]*(1+slip))) * 100 if entry_record.get("spy_open") and spy_out is not None and spy_out.valid else None
        payload = {"exit": exit_day, "price": exit_price, "quantity": qty, "pnl_usd": (exit_price-price)*qty, "spy_excess_pp": excess,
                   "note": "settled daily-bar PAPER; exact dates, not contemporaneous broker fills"}
        append(engine, cast(Table, storage.Result.__table__), [dict(key=exit_key,decision_key=decision.key,kind="closed",observed_at=now,payload_json=dumps(payload))])
    observed_tickers = {b.ticker for b in bars if b.day == latest}
    STATE.update(status="ok", at=now.isoformat(), latest_day=str(latest), covered=len(observed_tickers), expected=len(frozen["tickers"]), candidates=eligible_count)


def _single(engine: Engine, now: datetime) -> None:
    if not _RUNNING.acquire(blocking=False):
        return
    try:
        observe(engine, now)
    finally:
        _RUNNING.release()


async def run(engine: Engine, now: datetime) -> None:
    previous = STATE.get("checked_at")
    if isinstance(previous, datetime) and now - previous < timedelta(seconds=settings().monitor_interval_seconds):
        return
    STATE["checked_at"] = now
    try:
        await asyncio.wait_for(asyncio.to_thread(_single, engine, now), timeout=settings().monitor_timeout_seconds)
    except Exception as exc:
        STATE.update(status="failed", at=utc(now).isoformat(), error=type(exc).__name__)
        _logger.exception("calibration monitor failed; live desk preserved")


def view(engine: Engine, now: datetime) -> dict[str, Any]:
    storage.ensure(engine)
    with session_factory(engine)() as session:
        decisions = list(session.scalars(select(storage.Decision).where(storage.Decision.observed_at <= now).order_by(storage.Decision.observed_at.desc())))
        results = list(session.scalars(select(storage.Result).where(storage.Result.observed_at <= now)))
        bar_rows = list(session.scalars(select(storage.DailyBar).where(storage.DailyBar.observed_at <= now)))
    by_key = {(r.decision_key, r.kind): json.loads(r.payload_json) for r in results}
    rows: list[dict[str, Any]] = []  # Read-only JSON view adapter.
    for d in decisions:
        result = by_key.get((d.key, "closed"))
        rows.append({"ticker": d.ticker, "day": d.day, "status": "closed" if result is not None else "unfilled" if (d.key, "unfilled") in by_key else "open" if (d.key, "entry") in by_key else "pending", "result": result})
    frozen = report.load()
    latest = max((r.day for r in bar_rows if r.ticker == "SPY"), default=None)
    covered = len({r.ticker for r in bar_rows if r.day == latest and r.ticker in frozen["tickers"]})
    closed = [r["result"] for r in rows if r["result"] is not None]
    return {"report": frozen, "paper": rows[:settings().display_limit], "total": len(rows), "closed": len(closed),
            "pnl": sum(r["pnl_usd"] for r in closed) if closed else None,
            "matched": sum(r["spy_excess_pp"] is not None for r in closed), "covered": covered, "expected": len(frozen["tickers"]),
            "latest": latest, "job": {k: v for k,v in STATE.items() if k != "checked_at"},
            "market": classify(now, calendar()).mode.value}
