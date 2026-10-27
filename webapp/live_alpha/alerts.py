"""Private stored-snapshot adapter. Notify only; never creates a trading decision."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings, profile_path
from webapp.live_alpha import store
from webapp.live_alpha.inputs import as_utc


class AlertSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    poll_seconds: int = Field(gt=0)
    seen_ttl_seconds: int = Field(gt=0)
    max_seen: int = Field(gt=0)


def settings() -> AlertSettings:
    data = YAML(typ="safe").load(profile_path("live_alerts_v1.yaml").read_bytes())
    return AlertSettings(**data)


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    ticker: str
    instrument: Literal["stock", "long_call", "bull_call_debit"]
    label: str
    contracts: tuple[str, ...]
    underlying_ref: float
    underlying_stop: float
    underlying_target: float
    as_of: datetime
    expires_at: datetime


class Feed(BaseModel):
    model_config = ConfigDict(frozen=True)
    server_time: datetime
    status: str
    detail: str
    scan_id: str | None = None
    signals: tuple[Signal, ...] = ()


def _at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _signal(
    card: dict[str, Any], snapshot: dict[str, Any], now: datetime, deadline: datetime,
    started: datetime,
) -> Signal | None:
    # JSON is confined to this persistence adapter. Missing old metadata fails
    # closed; do not parse human evidence text to invent a quote or price age.
    cfg = load_live_alpha_settings()
    if (
        card.get("recommendation") != "BUY" or card.get("readiness") != "READY"
        or card.get("direction") != "up" or card.get("market_mode") != "LIVE"
    ):
        return None
    at = _at(card.get("decision_as_of"))
    ticker = card.get("ticker")
    policy = card.get("policy_version")
    opportunity = card.get("opportunity_id")
    if not (
        at and started <= at <= now and isinstance(ticker, str) and ticker
        and isinstance(policy, str) and policy and isinstance(opportunity, str)
        and opportunity == f"{now.astimezone(ZoneInfo(cfg.calendar.timezone)).date()}:{ticker}:up"
    ):
        return None
    instrument = card.get("instrument")
    plan = card.get("stock_plan")
    ages = snapshot.get("spot_age_seconds")
    if not isinstance(instrument, dict) or not isinstance(plan, dict) or not isinstance(ages, dict):
        return None
    chosen = instrument.get("preferred")
    if chosen not in ("stock", "long_call", "bull_call_debit"):
        return None
    age = _number(ages.get(ticker))
    if age is None or age < 0:
        return None
    deadline = min(deadline, started + timedelta(seconds=cfg.price.spot_max_age_seconds - age))
    contracts: tuple[str, ...] = ()
    if chosen != "stock":
        options = card.get("options")
        if not isinstance(options, list):
            return None
        selected = next((v for v in options if isinstance(v, dict) and v.get("kind") == chosen), None)
        if not selected or selected.get("readiness") != "READY":
            return None
        legs = selected.get("legs")
        if not isinstance(legs, list) or len(legs) != (1 if chosen == "long_call" else 2):
            return None
        symbols = []
        for leg in legs:
            if not isinstance(leg, dict):
                return None
            fetched = _at(leg.get("fetched_at"))
            symbol = leg.get("option_symbol")
            if fetched is None or fetched > now or not isinstance(symbol, str) or not symbol:
                return None
            deadline = min(deadline, fetched + timedelta(seconds=cfg.option_plan.quote_max_age_seconds))
            symbols.append(symbol)
        contracts = tuple(symbols)
    levels = [_number(plan.get(name)) for name in ("entry_ref", "stop", "target")]
    if deadline <= now or any(v is None or v <= 0 for v in levels):
        return None
    entry, stop, target = levels
    assert entry is not None and stop is not None and target is not None
    key = hashlib.sha256(f"{policy}|{opportunity}|{chosen}".encode()).hexdigest()
    return Signal(
        key=key, ticker=ticker, instrument=chosen,
        label={"stock": "HİSSE ALIM", "long_call": "CALL", "bull_call_debit": "CALL SPREAD"}[chosen],
        contracts=contracts, underlying_ref=entry, underlying_stop=stop, underlying_target=target,
        as_of=at, expires_at=deadline,
    )


def feed(engine: Engine, now: datetime) -> Feed:
    now = as_utc(now)
    cfg = load_live_alpha_settings()
    session = classify(now, cfg.calendar)
    if session.mode is not MarketMode.LIVE or not session.session_date or not session.close_et:
        return Feed(server_time=now, status="not_live", detail="Seans açık değil; uyarı beklemede.")
    scan = store.latest_scan(engine) if inspect(engine).has_table("alfa_live_scan") else None
    if scan is None:
        return Feed(server_time=now, status="no_scan", detail="İlk tarama bekleniyor.")
    started, finished = as_utc(scan.started_at), as_utc(scan.finished_at)
    if (
        scan.status == "failed" or scan.market_mode != "LIVE" or not started <= finished <= now
        or started.astimezone(ZoneInfo(cfg.calendar.timezone)).date() != session.session_date
        or scan.snapshot.get("flow_current") is not True
    ):
        return Feed(server_time=now, status="unavailable", detail="Güncel seans verisi doğrulanamadı.")
    close = datetime.combine(session.session_date, session.close_et, ZoneInfo(cfg.calendar.timezone))
    deadline = min(started + timedelta(seconds=3 * cfg.cycle.live_seconds), close)
    if deadline <= now:
        return Feed(server_time=now, status="stale", detail="Tarama eski; yeni veri bekleniyor.")
    cards = scan.snapshot.get("cards", [])
    signals = tuple(
        signal for card in cards if isinstance(card, dict)
        if (signal := _signal(card, scan.snapshot, now, deadline, started)) is not None
    ) if isinstance(cards, list) else ()
    return Feed(server_time=now, status="current", detail="Güncel tarama izleniyor.", scan_id=scan.scan_id, signals=signals)
