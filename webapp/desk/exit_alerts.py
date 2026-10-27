"""Recent recorded exit reviews only; never sends or places an order."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab.flow import utc
from webapp.board.db import session_factory
from webapp.desk import db
from webapp.desk.positions import manual_plans, manual_trades
from webapp.desk.settings import settings
from webapp.live_alpha.store import LiveEvent, LivePaper


@dataclass(frozen=True)
class ExitNotice:
    key: str
    ticker: str
    instrument: str
    label: str
    message: str
    at: datetime
    expires: datetime


def recent(engine: Engine, now: datetime) -> tuple[ExitNotice, ...]:
    now = utc(now)
    ttl = timedelta(seconds=settings().exit_alert_ttl_seconds)
    cutoff = now - ttl
    result = []
    with session_factory(engine)() as session:
        if inspect(engine).has_table("alfa_live_event"):
            events = list(session.scalars(select(LiveEvent).where(LiveEvent.kind.in_(["paper_exit", "exit_signalled"]), LiveEvent.at >= cutoff, LiveEvent.at <= now)))
            for event in events:
                data = json.loads(event.detail_json)
                p = session.get(LivePaper, data.get("paper_id", ""))
                if not p or not p.entry_at:
                    continue
                if utc(p.entry_at) > utc(event.at):
                    continue
                if event.kind == "exit_signalled" and p.state != "EXIT_SIGNALLED":
                    continue
                if event.kind == "paper_exit" and (p.state != "CLOSED_SIMULATED" or p.exit_at is None or p.exit_price is None or utc(p.exit_at) > now):
                    continue
                if p.instrument not in ("stock", "long_call", "long_put", "bull_call_debit", "bear_put_debit"):
                    continue
                kind = "exit_pending" if event.kind == "exit_signalled" else "closed"
                message = f"PAPER · {p.ticker}: {data.get('reason', p.exit_reason or 'çıkış kaydı')}"
                message += "; çıkış fiyatı bekleniyor." if kind == "exit_pending" else "; simüle kapanış kaydedildi."
                result.append(ExitNotice(db.digest((p.paper_id, kind, data.get("reason"))), p.ticker, p.instrument,
                    "PAPER ÇIKIŞ İNCELEMESİ", message, utc(event.at), utc(event.at) + ttl))
        if inspect(engine).has_table("alfa_desk_sample"):
            opened = {p.id: p for p in manual_trades(engine, now)}
            plans = manual_plans(engine, now)
            latest = {}
            for row in session.scalars(select(db.Sample).where(db.Sample.scope == "manual", db.Sample.at >= cutoff, db.Sample.at <= now).order_by(db.Sample.at)):
                latest[row.position_id] = row
            for identifier, sample in latest.items():
                data = json.loads(sample.payload_json)
                plan = plans.get(identifier)
                if identifier not in opened or plan is None or data.get("plan_id") != plan.key or data.get("action") != "EXIT_REVIEW":
                    continue
                trade = opened[identifier]
                instrument = {"shares": "stock", "call": "long_call", "put": "long_put"}.get(trade.instrument)
                if instrument is None:
                    continue
                result.append(ExitNotice(db.digest(("manual", identifier, plan.key, data.get("reason"))), trade.ticker,
                    instrument, "MANUEL ÇIKIŞ İNCELEMESİ", f"Manuel kayıt · {trade.ticker}: {data.get('reason')}. Gerçek satış kaydedilmedi.",
                    utc(sample.at), utc(sample.at) + ttl))
    return tuple(result)
