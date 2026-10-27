from __future__ import annotations

import hashlib
from datetime import date
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path


class Fold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    train_end: date
    start: date
    end: date


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    thresholds: dict[str, tuple[float, ...]]
    lookbacks: tuple[int, ...]
    volume_gates: tuple[float, ...]
    regimes: tuple[str, ...]
    horizons: tuple[int, ...]
    sizing_modes: tuple[str, ...]
    slots: tuple[int, ...]
    atr_sessions: int = Field(gt=0)
    volume_sessions: int = Field(gt=0)
    compression_fast: int = Field(gt=0)
    compression_slow: int = Field(gt=0)
    market_ma: int = Field(gt=0)
    recovery_chase_atr: float = Field(gt=0)
    breakout_chase_atr: float = Field(gt=0)
    capital_usd: float = Field(gt=0)
    position_usd: float = Field(gt=0)
    risk_usd: float = Field(gt=0)
    risk_atr_multiple: float = Field(gt=0)
    slippage_bps: float = Field(ge=0, lt=10000)
    sensitivity_bps: tuple[float, ...]
    train_start: date
    folds: tuple[Fold, ...]
    test_start: date
    test_end: date
    min_train_closed: int = Field(gt=0)
    min_validation_closed: int = Field(gt=0)
    min_fold_closed: int = Field(gt=0)
    min_positive_folds: int = Field(gt=0)
    min_test_closed: int = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0)
    bootstrap_reps: int = Field(gt=0)
    bootstrap_block_sessions: int = Field(gt=0)
    bootstrap_seed: int
    bootstrap_percentile: float = Field(gt=0, lt=100)
    flow_score_min: float
    flow_count_min: int = Field(gt=0)
    prospective_start: date
    api_max_age_seconds: int = Field(gt=0)
    api_record_limit: int = Field(gt=0)
    monitor_seconds: int = Field(gt=0)
    monitor_timeout_seconds: float = Field(gt=0)
    display_limit: int = Field(gt=0)
    arms: tuple[str, ...]
    iv_max_rank: float
    peer_min_alignment: float
    profile_hash: str


@lru_cache(maxsize=1)
def settings() -> Settings:
    raw = profile_path("tournament_v1.yaml").read_bytes()
    return Settings(**YAML(typ="safe").load(raw), profile_hash=hashlib.sha256(raw).hexdigest())
