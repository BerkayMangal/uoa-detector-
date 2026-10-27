"""Failure-isolated prospective samples and reference snapshots after a live scan."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime
from typing import cast

from sqlalchemy import Table, inspect, or_, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.flow import timestamp, utc
from webapp.alpha_lab.readers import spots_at
from webapp.board.baskets import load_basket_settings
from webapp.board.db import session_factory
from webapp.board.lag_record import AlfaLagRecord
from webapp.desk import db, positions
from webapp.desk.settings import settings
from webapp.live_alpha.store import LivePaper, ScanRow

STATE: dict[str, object] = {}
_RUNNING = threading.Lock()
_logger = logging.getLogger(__name__)


def _peers(engine: Engine, ticker: str, at: datetime) -> tuple[str, ...]:
    if inspect(engine).has_table("alfa_lag_record"):
        with session_factory(engine)() as session:
            row = session.scalar(select(AlfaLagRecord).where(AlfaLagRecord.ticker == ticker, AlfaLagRecord.recorded_at <= at).order_by(AlfaLagRecord.recorded_at.desc()).limit(1))
            if row:
                return tuple(dict.fromkeys(t for t in row.peers.split(",") if t and t != ticker))
    basket = load_basket_settings().basket_of(ticker)
    return tuple(t for t in basket.members if t != ticker) if basket else ()


def _point(raw: object, at: datetime) -> dict[str, object] | None:
    cfg = settings()
    if not isinstance(raw, dict):
        return None
    value, source = positions.number(raw.get("spot")), timestamp(raw.get("source_at"))
    from zoneinfo import ZoneInfo

    day = at.astimezone(ZoneInfo(load_live_alpha_settings().calendar.timezone)).date().isoformat()
    if value is None or value <= 0 or source is None or raw.get("session") != day or not 0 <= (at - source).total_seconds() <= cfg.price_max_age_seconds:
        return None
    return {"value": value, "source_at": source.isoformat()}


def capture_benchmarks(engine: Engine, scan: ScanRow, observed_at: datetime) -> int:
    db.ensure(engine)
    at, cfg = utc(scan.started_at), settings()
    if utc(observed_at) < at:
        return 0
    raw = scan.snapshot.get("price_observations", {})
    with session_factory(engine)() as session:
        changed = list(session.scalars(select(LivePaper).where(or_(LivePaper.entry_at == at, LivePaper.exit_at == at))))
        known = {r.key: json.loads(r.payload_json) for r in session.scalars(select(db.Benchmark).where(db.Benchmark.paper_id.in_([p.paper_id for p in changed])))}
    rows = []
    for paper in changed:
        if paper.entry_at is None or paper.quantity is None or paper.quantity <= 0:
            continue
        entry_key = db.digest((paper.paper_id, "entry"))
        entry = known.get(entry_key)
        kinds = ["entry"] if utc(paper.entry_at) == at else []
        if paper.state == "CLOSED_SIMULATED" and paper.exit_at and utc(paper.exit_at) == at:
            kinds.append("exit")
        for kind in kinds:
            peer_names = tuple(entry.get("peers", [])) if entry else _peers(engine, paper.ticker, at) if kind == "entry" else ()
            peer_spots = spots_at(engine, peer_names, at)
            own = _point(raw.get(paper.ticker), at) if isinstance(raw, dict) else None
            spy = _point(raw.get("SPY"), at) if isinstance(raw, dict) else None
            points: dict[str, object] = {paper.ticker: own, "SPY": spy}
            for name in peer_names:
                p = peer_spots.get(name)
                points[name] = _point({"spot": p.spot, "source_at": p.fetched_at.isoformat(), "session": p.trade_date.isoformat() if p.trade_date else None}, at) if p else None
            payload = {"paper_id": paper.paper_id, "kind": kind, "event_at": at.isoformat(),
                "observed_at": utc(observed_at).isoformat(), "ticker": paper.ticker,
                "direction": paper.direction, "peers": peer_names, "points": points,
                "profile_hash": cfg.profile_hash, "max_skew_seconds": cfg.benchmark_pair_skew_seconds,
                "basis": "cached price observations available to the simulation cycle; not synchronous fills"}
            rows.append(dict(key=db.digest((paper.paper_id, kind)), paper_id=paper.paper_id,
                kind=kind, at=observed_at, payload_json=db.dumps(payload)))
            if kind == "entry" and entry is None:
                entry = payload
    return db.append(engine, cast(Table, db.Benchmark.__table__), rows)


def observe(engine: Engine, scan: ScanRow, now: datetime) -> None:
    if not _RUNNING.acquire(blocking=False):
        raise RuntimeError("previous desk sample still running")
    try:
        view = positions.build(engine, now)
        rows = [dict(key=db.digest((p.scope, p.id, scan.scan_id)), position_id=p.id, scope=p.scope,
                     at=now, payload_json=db.dumps(asdict(p))) for p in (*view.paper.rows, *view.manual.rows)]
        samples = db.append(engine, cast(Table, db.Sample.__table__), rows)
        benchmarks = capture_benchmarks(engine, scan, now)
        STATE.clear()
        STATE.update(status="ok", at=utc(now).isoformat(), paper_positions=len(view.paper.rows),
                     manual_positions=len(view.manual.rows), samples=samples, benchmark_snapshots=benchmarks)
    finally:
        _RUNNING.release()


async def run(engine: Engine, scan: ScanRow, now: datetime) -> None:
    try:
        await asyncio.wait_for(asyncio.to_thread(observe, engine, scan, now), timeout=settings().cycle_timeout_seconds)
    except Exception as exc:
        STATE.clear()
        STATE.update(status="failed", at=utc(now).isoformat(), error=type(exc).__name__)
        _logger.exception("desk sample failed; legacy cycle preserved")
