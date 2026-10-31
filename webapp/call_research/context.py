"""Freeze prior settled-session context as actually known at a call's timestamp."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from typing import cast

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML
from sqlalchemy import Table, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import previous_session_before
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import session_factory
from webapp.board.ticker_info import AlfaTickerInfo
from webapp.calibration.settings import calendar
from webapp.call_research.coverage import Observed
from webapp.call_research.scorecard import number
from webapp.call_research.settings import ROOT, settings
from webapp.call_research.storage import CallContext
from webapp.opening.model import utc
from webapp.opening.storage import OpeningDecision
from webapp.research_readiness import settled_day

PROFILE = ROOT / "profiles/call_context_v1.yaml"


class ContextSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    capture_limit: int
    start_day: date
    known_sector_overrides: dict[str, str]
    sector_etfs: dict[str, str]


@cache
def config() -> ContextSettings:
    return ContextSettings.model_validate(YAML(typ="safe").load(PROFILE.read_bytes()))


@dataclass(frozen=True)
class Point:
    ticker: str
    return_pct: float | None
    relative_pp: float | None


@dataclass(frozen=True)
class ContextView:
    previous_day: date | None
    day: date | None
    sector_etf: str | None
    points: tuple[Point, ...]
    version: str


def capture(engine: Engine, points: tuple[Observed, ...], now: datetime) -> int:
    if not inspect(engine).has_table(OpeningDecision.__tablename__):
        return 0
    panel = {(p.bar.ticker, p.bar.day): p for p in points if p.verified}
    cfg = config()
    with session_factory(engine)() as session:
        calls = session.scalars(
            select(OpeningDecision)
            .outerjoin(CallContext, OpeningDecision.key == CallContext.decision_key)
            .where(
                CallContext.key.is_(None),
                OpeningDecision.day >= cfg.start_day,
                OpeningDecision.published_at <= now,
            )
            .order_by(OpeningDecision.published_at.asc(), OpeningDecision.key.asc())
            .limit(cfg.capture_limit)
        ).all()
        has_info = inspect(engine).has_table(AlfaTickerInfo.__tablename__)
        infos = (
            {
                r.ticker: r
                for r in session.scalars(
                    select(AlfaTickerInfo).where(
                        AlfaTickerInfo.ticker.in_({c.ticker for c in calls})
                    )
                )
            }
            if has_info and calls
            else {}
        )
        saved = []
        for call in calls:
            key = digest((cfg.version, call.key))
            at = utc(call.published_at)
            last = settled_day(at)
            previous = previous_session_before(last, calendar()) if last else None
            sector = cfg.known_sector_overrides.get(call.ticker)
            if sector is None:
                info = infos.get(call.ticker)
                if info and utc(info.fetched_at) <= at and info.sector:
                    sector = cfg.sector_etfs.get(info.sector.lower().replace("_", " ").strip())
            values: dict[str, float | None] = {}
            for name in dict.fromkeys(
                (call.ticker, *settings().benchmarks, *([sector] if sector else []))
            ):
                first = panel.get((name, previous)) if previous else None
                end = panel.get((name, last)) if last else None
                values[name] = (
                    (end.bar.close / first.bar.close - 1) * 100
                    if first
                    and end
                    and first.bar.valid
                    and end.bar.valid
                    and first.available_at <= at
                    and end.available_at <= at
                    else None
                )
            own = values[call.ticker]
            payload = {
                "version": cfg.version,
                "profile_hash": hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
                "call_at": at,
                "previous_day": previous,
                "day": last,
                "sector_etf": sector,
                "scope": "prior settled daily sessions; not intraday call performance",
                "points": [
                    {
                        "ticker": name,
                        "return_pct": value,
                        "relative_pp": own - value
                        if own is not None and value is not None
                        else None,
                    }
                    for name, value in values.items()
                ],
            }
            saved.append(
                dict(key=key, decision_key=call.key, observed_at=now, payload_json=dumps(payload))
            )
    return append(engine, cast(Table, CallContext.__table__), saved)


def view(engine: Engine, key: str, now: datetime) -> ContextView | None:
    if not inspect(engine).has_table(CallContext.__tablename__):
        return None
    with session_factory(engine)() as session:
        row = session.scalar(
            select(CallContext)
            .where(CallContext.decision_key == key, CallContext.observed_at <= now)
            .order_by(CallContext.observed_at)
            .limit(1)
        )
        if row is None:
            return None
        raw = json.loads(row.payload_json)
        return ContextView(
            date.fromisoformat(raw["previous_day"]) if raw.get("previous_day") else None,
            date.fromisoformat(raw["day"]) if raw.get("day") else None,
            raw.get("sector_etf"),
            tuple(
                Point(str(p["ticker"]), number(p.get("return_pct")), number(p.get("relative_pp")))
                for p in raw.get("points", [])
            ),
            str(raw.get("version", "unknown")),
        )
