"""Frozen typed opening instructions and the exchange clock."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.calendar import classify, close_time
from uoa_detector.live_alpha.settings import load_live_alpha_settings, profile_path


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    start_day: date
    preparation_minutes: int = Field(gt=0)
    entry_delay_minutes: int = Field(gt=0)
    checkpoint_minutes: int = Field(gt=0)
    source_max_age_seconds: int = Field(gt=0)
    flow_max_age_seconds: int = Field(gt=0)
    reference_window_seconds: int = Field(gt=0)
    intraday_minutes: tuple[int, ...]
    close_sessions: tuple[int, ...]
    cycle_timeout_seconds: int = Field(gt=0)
    query_limit: int = Field(gt=0)
    history_limit: int = Field(gt=0)
    display_limit: int = Field(gt=0)
    profile_hash: str


@cache
def settings() -> Settings:
    raw = profile_path("opening_desk_v1.yaml").read_bytes()
    return Settings(**YAML(typ="safe").load(raw), profile_hash=hashlib.sha256(raw).hexdigest())


@cache
def code_hash() -> str:
    from pathlib import Path

    root = Path(__file__).parent
    return hashlib.sha256(
        b"".join(p.name.encode() + p.read_bytes() for p in sorted(root.glob("*.py")))
    ).hexdigest()


def utc(at: datetime) -> datetime:
    # SQLite strips timezone metadata on database reads only.
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


@dataclass(frozen=True)
class Clock:
    day: date
    phase: str
    checkpoint: str | None
    preparation: datetime | None
    opening: datetime | None
    assessment: datetime | None
    closing: datetime | None
    reason: str


def clock(at: datetime) -> Clock:
    cfg, base = settings(), load_live_alpha_settings()
    session = classify(at, base.calendar)
    day = session.et_now.date()
    if session.session_date is None or day < cfg.start_day:
        return Clock(
            day,
            "closed",
            None,
            None,
            None,
            None,
            None,
            session.reason or "İleriye dönük kayıt henüz başlamadı.",
        )
    zone = ZoneInfo(base.calendar.timezone)
    opening = datetime.combine(day, base.calendar.regular_open, zone).astimezone(UTC)
    prep = opening - timedelta(minutes=cfg.preparation_minutes)
    assessment = opening + timedelta(minutes=cfg.entry_delay_minutes)
    closing = datetime.combine(day, close_time(day, base.calendar), zone).astimezone(UTC)
    if at < prep:
        phase, reason = "before", "Hazırlık saati bekleniyor; yeni işlem yok."
    elif at < opening:
        phase, reason = "preparation", "Hazırlık listesi; açılış öncesi yeni AL önerisi yok."
    elif at < assessment:
        phase, reason = (
            "settling",
            "İlk 15 dakika: yeni giriş için bekle; mevcut pozisyonun riskini izle.",
        )
    elif at < closing:
        phase, reason = (
            "assessment",
            "Açılış sonrası değerlendirme; yalnız taze ve kayıtlı planlar.",
        )
    else:
        phase, reason = "closed", "Seans kapandı; yeni giriş yok."
    checkpoint = (
        "preparation"
        if phase == "preparation"
        else "post_open"
        if assessment <= at < assessment + timedelta(minutes=cfg.checkpoint_minutes)
        else None
    )
    return Clock(day, phase, checkpoint, prep, opening, assessment, closing, reason)


def next_wait(at: datetime, normal: float) -> float:
    c = clock(at)
    upcoming = [
        (t - at).total_seconds() for t in (c.preparation, c.opening, c.assessment) if t and t > at
    ]
    return min(normal, *upcoming) if upcoming else normal


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)
    entry: float
    stop: float
    target: float
    quantity: int
    risk_usd: float
    instrument: str
    contracts: tuple[str, ...] = ()
    debit: float | None = None
    roundtrip_cost_pct: float | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    text: str
    source_at: datetime | None = None


class Advice(BaseModel):
    model_config = ConfigDict(frozen=True)
    ticker: str
    action: Literal["BUY", "SELL", "WAIT", "PAPER_EXIT", "REVIEW"]
    scope: str = "idea"
    position_id: str | None = None
    instrument: str = "none"
    direction: str = "up"
    headline: str
    reasons: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    counter_argument: str = ""
    invalidation: str = ""
    plan: Plan | None = None
    source_card_hash: str = ""
    source_policy: str = ""
    source_profile_hash: str = ""
    source_at: datetime | None = None
    expires_at: datetime | None = None
