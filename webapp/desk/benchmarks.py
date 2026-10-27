"""Cached-reference comparisons, with coverage for every entered PAPER outcome."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab.flow import timestamp, utc
from webapp.board.db import session_factory
from webapp.desk import db
from webapp.desk.positions import number
from webapp.live_alpha import store


@dataclass(frozen=True)
class Comparison:
    ticker: str
    paper_id: str
    direction: str
    pnl: float | None
    spy_excess: float | None
    peer_excess: float | None
    entry_at: str
    exit_at: str
    note: str


@dataclass(frozen=True)
class ReferenceView:
    total: int
    spy_known: int
    peers_known: int
    rows: tuple[Comparison, ...]


def _return(entry: dict[str, Any], exit_: dict[str, Any], name: str, anchor: str) -> float | None:
    points = []
    for payload in (entry, exit_):
        values = payload.get("points", {})
        point, own = values.get(name), values.get(anchor)
        if not isinstance(point, dict) or not isinstance(own, dict):
            return None
        value, source, paired = number(point.get("value")), timestamp(point.get("source_at")), timestamp(own.get("source_at"))
        event = timestamp(payload.get("event_at"))
        if value is None or value <= 0 or source is None or paired is None or event is None or source > event:
            return None
        if abs((source - paired).total_seconds()) > payload.get("max_skew_seconds", 0):
            return None
        points.append(value)
    return (points[1] / points[0] - 1) * 100


def build(engine: Engine, now: datetime) -> ReferenceView:
    if not inspect(engine).has_table("alfa_live_paper"):
        return ReferenceView(0, 0, 0, ())
    papers = [p for p in store.papers(engine, ["CLOSED_SIMULATED"]) if p.entry_at and p.entry_price and p.quantity]
    snapshots: dict[str, Any] = {}
    if inspect(engine).has_table("alfa_desk_benchmark"):
        with session_factory(engine)() as session:
            snapshots = {r.key: json.loads(r.payload_json) for r in session.scalars(select(db.Benchmark).where(db.Benchmark.at <= now))}
    result = []
    for p in papers:
        entry, exit_ = snapshots.get(db.digest((p.paper_id, "entry"))), snapshots.get(db.digest((p.paper_id, "exit")))
        spy = peers = None
        note = "Giriş/çıkış referans gözlemi eksik; geçmiş fiyatla doldurulmaz."
        if entry and exit_:
            own = _return(entry, exit_, p.ticker, p.ticker)
            benchmark = _return(entry, exit_, "SPY", p.ticker)
            sign = 1 if p.direction == "up" else -1
            spy = sign * (own - benchmark) if own is not None and benchmark is not None else None
            names = entry.get("peers", [])
            returns = [_return(entry, exit_, name, p.ticker) for name in names]
            if own is not None and names and all(r is not None for r in returns):
                peers = sign * (own - sum(r for r in returns if r is not None) / len(returns))
            note = "Önbellek referansları; masraf öncesi yönlü dayanak farkı, opsiyon getirisi değil."
        result.append(Comparison(p.ticker, p.paper_id, p.direction, number(p.pnl_usd), spy, peers,
            utc(p.entry_at).isoformat() if p.entry_at else "", utc(p.exit_at).isoformat() if p.exit_at else "", note))
    return ReferenceView(len(result), sum(r.spy_excess is not None for r in result), sum(r.peer_excess is not None for r in result), tuple(result))
