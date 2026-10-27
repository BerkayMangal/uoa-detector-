"""Separate recorded PAPER/manual risk; never infers an unrecorded broker holding."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.readers import spots_at
from webapp.board.baskets import load_basket_settings
from webapp.board.db import session_factory
from webapp.desk import db
from webapp.desk.settings import settings
from webapp.journal import TradeRow
from webapp.live_alpha import store
from webapp.live_alpha.inputs import as_utc


@dataclass(frozen=True)
class Position:
    id: str
    scope: str
    ticker: str
    instrument: str
    direction: str
    state: str
    quantity: float | None
    entry: float | None
    spot: float | None
    spot_at: datetime | None
    mark: float | None
    pnl: float | None
    stop: float | None
    target: float | None
    stop_distance_pct: float | None
    allocation: float | None
    risk: float | None
    group: str
    action: str
    reason: str
    opportunity: str | None
    plan_id: str | None


@dataclass(frozen=True)
class Concentration:
    group: str
    allocation: float
    risk: float | None
    count: int
    share: float


@dataclass(frozen=True)
class Account:
    scope: str
    rows: tuple[Position, ...]
    allocation: float | None
    risk: float | None
    unknown_risk: int
    groups: tuple[Concentration, ...]


@dataclass(frozen=True)
class DeskView:
    paper: Account
    manual: Account
    capital: float
    capital_exceeded: bool
    risk_warning: bool
    live: bool
    as_of: datetime
    concentration_warning_pct: float
    price_max_age_seconds: int


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _positive(value: object) -> float | None:
    n = number(value)
    return n if n is not None and n > 0 else None


def manual_trades(engine: Engine, now: datetime | None = None) -> list[TradeRow]:
    if not inspect(engine).has_table("trade"):
        return []
    with session_factory(engine)() as session:
        query = select(TradeRow).where(TradeRow.status == "open")
        if now is not None:
            query = query.where(TradeRow.created_at <= now, TradeRow.entry_ts <= now)
        return list(session.scalars(query))


def manual_plans(engine: Engine, now: datetime) -> dict[str, db.ManualPlan]:
    db.ensure(engine)
    with session_factory(engine)() as session:
        rows = session.scalars(select(db.ManualPlan).where(db.ManualPlan.created_at <= now).order_by(db.ManualPlan.created_at, db.ManualPlan.key))
        return {row.trade_id: row for row in rows}


def _account(scope: str, rows: list[Position]) -> Account:
    funded = [p for p in rows if p.state != "PAPER_PENDING"]
    allocations = [p.allocation for p in funded if p.allocation is not None]
    risks = [p.risk for p in funded if p.risk is not None]
    total = sum(allocations)
    grouped: dict[str, list[Position]] = defaultdict(list)
    for p in funded:
        grouped[p.group].append(p)
    groups = []
    for group, positions in grouped.items():
        cash = sum(p.allocation or 0 for p in positions)
        known = [p.risk for p in positions if p.risk is not None]
        groups.append(Concentration(group, cash, sum(known) if known else None, len(positions), cash / total * 100 if total > 0 else 0))
    return Account(scope, tuple(rows), total if allocations else 0.0 if not funded else None, sum(risks) if risks else 0.0 if not funded else None,
                   sum(p.risk is None for p in funded), tuple(sorted(groups, key=lambda g: -g.allocation)))


def build(engine: Engine, now: datetime) -> DeskView:
    now = as_utc(now)
    cfg = settings()
    calendar = load_live_alpha_settings().calendar
    current = classify(now, calendar)
    today = now.astimezone(ZoneInfo(calendar.timezone)).date()
    papers = store.papers(engine, ["PAPER_PENDING", "PAPER_OPEN", "EXIT_SIGNALLED"]) if inspect(engine).has_table("alfa_live_paper") else []
    trades, plans = manual_trades(engine, now), manual_plans(engine, now)
    papers = [p for p in papers if as_utc(p.created_at) <= now and (p.entry_at is None or as_utc(p.entry_at) <= now)]
    spots = spots_at(engine, [p.ticker for p in papers] + [p.ticker for p in trades], now)
    baskets = load_basket_settings()
    output: dict[str, list[Position]] = {"paper": [], "manual": []}
    # Any at this adapter boundary is only the existing immutable plan JSON.
    raw: list[tuple[str, Any]] = [("paper", p) for p in papers] + [("manual", t) for t in trades]
    for scope, p in raw:
        paper = scope == "paper"
        identity = p.paper_id if paper else p.id
        source = spots.get(p.ticker)
        valid_spot = source is not None and source.trade_date == today and 0 <= (now - source.fetched_at).total_seconds() <= cfg.price_max_age_seconds
        spot = source.spot if source and valid_spot else None
        direction = p.direction if paper else "up" if p.direction == "bullish" else "down"
        plan = json.loads(p.plan_json) if paper else {}
        sp = plan.get("stock_plan") or {}
        attachment = plans.get(identity) if not paper else None
        stop = _positive(sp.get("stop")) if paper else attachment.stop if attachment else None
        target = _positive(sp.get("target")) if paper else attachment.target if attachment else None
        instrument = p.instrument
        stock = instrument in ("stock", "shares")
        quantity = _positive(p.quantity if paper else p.contracts)
        entry = _positive(p.entry_price)
        funded = not paper or p.state != "PAPER_PENDING"
        multiplier = _positive((plan.get("structure") or {}).get("multiplier", 100)) if paper and not stock else 1.0 if stock else 100.0
        commission = number((plan.get("structure") or {}).get("commission_usd", 0)) if paper else 0.0
        allocation = entry * quantity * multiplier if funded and entry and quantity and multiplier else None
        risk = None
        if allocation is not None and quantity and multiplier:
            if stock and stop and entry:
                risk = max(0, (entry - stop) * (1 if direction == "up" else -1) * quantity)
            elif not stock and (paper or instrument in ("call", "put")):
                risk = allocation + (commission or 0) * quantity
        mark = None
        if funded:
            if stock:
                mark = spot
            elif paper and p.last_mark_at and 0 <= (now - as_utc(p.last_mark_at)).total_seconds() <= cfg.price_max_age_seconds:
                mark = number(p.last_mark)
        pnl = ((mark - entry) * quantity * (multiplier or 1) * (-1 if stock and direction == "down" else 1) - (commission or 0) * quantity) if mark is not None and entry and quantity else None
        if stock and paper and mark is not None and entry and quantity:
            slip = load_live_alpha_settings().stock_plan.slippage_bps / 10000
            pnl = (mark * (1 - slip) - entry) * quantity
        reason = str(p.exit_reason or "") if paper else ""
        action = "EXIT_REVIEW" if paper and p.state == "EXIT_SIGNALLED" else "WAIT" if not funded else "MONITOR"
        if funded and spot is None:
            action = "DATA_WAIT" if action != "EXIT_REVIEW" else action
        elif funded and spot is not None and current.mode is MarketMode.LIVE:
            if stop and ((direction == "up" and spot <= stop) or (direction == "down" and spot >= stop)):
                action, reason = "EXIT_REVIEW", "stop seviyesi gözlendi"
            elif target and ((direction == "up" and spot >= target) or (direction == "down" and spot <= target)):
                action, reason = "EXIT_REVIEW", "hedef seviyesi gözlendi"
        if not paper and not attachment:
            reason = "Dayanak stop/hedef planı eklenmemiş."
        if stock and direction == "down":
            allocation = risk = pnl = None
            action, reason = "DATA_WAIT", "Açığa satış pozisyonu varsayılmıyor; kayıt tarafı doğrulanmalı."
        if not paper and instrument not in ("shares", "call", "put"):
            allocation = risk = pnl = None
            action, reason = "DATA_WAIT", "Araç ve çarpan doğrulanamıyor."
        if not paper and p.direction not in ("bullish", "bearish"):
            risk = pnl = None
            action, reason = "DATA_WAIT", "Kayıttaki işlem yönü doğrulanamıyor."
        allocation, risk, pnl = number(allocation), number(risk), number(pnl)
        basket = baskets.basket_of(p.ticker)
        group = basket.name if basket else f"{p.ticker} · grup tanımsız"
        output[scope].append(Position(identity, scope, p.ticker, instrument, direction,
            p.state if paper else p.status, quantity, entry, spot, source.fetched_at if source else None,
            mark, pnl, stop, target, (spot - stop) / spot * 100 * (1 if direction == "up" else -1) if spot and stop else None,
            allocation, risk, group, action, reason, p.opportunity_id if paper else None,
            attachment.key if attachment else None))
    pa, ma = _account("paper", output["paper"]), _account("manual", output["manual"])
    return DeskView(pa, ma, cfg.paper_capital_usd,
        pa.allocation is not None and pa.allocation > cfg.paper_capital_usd,
        pa.risk is not None and pa.risk / cfg.paper_capital_usd * 100 > cfg.planned_risk_warning_pct,
        current.mode is MarketMode.LIVE, now, cfg.concentration_warning_pct, cfg.price_max_age_seconds)
