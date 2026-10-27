from __future__ import annotations

import hashlib
from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import (
    CalendarSettings,
    load_live_alpha_settings,
    profile_path,
)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    lookbacks: tuple[int, ...]
    min_lag_pp: tuple[float, ...]
    min_recovery_atr: tuple[float, ...]
    min_volume_ratio: tuple[float, ...]
    regimes: tuple[Literal["any", "positive", "above_ma20"], ...]
    max_chase_atr: tuple[float, ...]
    atr_sessions: int = Field(gt=0)
    volume_sessions: int = Field(gt=0)
    market_ma_sessions: int = Field(gt=0)
    primary_sessions: int = Field(gt=0)
    secondary_sessions: tuple[int, ...]
    capital_usd: float = Field(gt=0)
    position_usd: float = Field(gt=0)
    max_positions: int = Field(gt=0)
    slippage_bps: float = Field(ge=0, lt=10000)
    sensitivity_bps: tuple[float, ...]
    min_closed: int = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0)
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    prospective_start: date
    monitor_timeout_seconds: float = Field(gt=0)
    monitor_interval_seconds: int = Field(gt=0)
    display_limit: int = Field(gt=0)
    historical_holidays_2025: tuple[date, ...]
    profile_hash: str


@lru_cache(maxsize=1)
def settings() -> Settings:
    raw = profile_path("calibration_v1.yaml").read_bytes()
    return Settings(**YAML(typ="safe").load(raw), profile_hash=hashlib.sha256(raw).hexdigest())


@lru_cache(maxsize=1)
def calendar() -> CalendarSettings:
    cal = load_live_alpha_settings().calendar
    return cal.model_copy(update={"years": (2025, *cal.years), "holidays": (*settings().historical_holidays_2025, *cal.holidays)})
