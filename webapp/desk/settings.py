from __future__ import annotations

import hashlib
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path


class DeskSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    paper_capital_usd: float = Field(gt=0)
    planned_risk_warning_pct: float = Field(gt=0)
    concentration_warning_pct: float = Field(gt=0, le=100)
    price_max_age_seconds: int = Field(gt=0)
    benchmark_pair_skew_seconds: int = Field(ge=0)
    exit_alert_ttl_seconds: int = Field(gt=0)
    cycle_timeout_seconds: float = Field(gt=0)
    display_limit: int = Field(gt=0)
    scenario_moves: tuple[float, ...]
    scenario_iv_shifts: tuple[float, ...]
    scenario_sessions: tuple[int, ...]
    scenario_rate: float
    scenario_dividend_yield: float
    scenario_iv_min: float = Field(gt=0)
    scenario_iv_max: float = Field(gt=0)
    scenario_solver_iterations: int = Field(gt=0)
    profile_hash: str = ""


@lru_cache(maxsize=1)
def settings() -> DeskSettings:
    raw = profile_path("desk_v1.yaml").read_bytes()
    return DeskSettings(**YAML(typ="safe").load(raw), profile_hash=hashlib.sha256(raw).hexdigest())
