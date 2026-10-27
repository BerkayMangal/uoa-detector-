"""Frozen pilot settings; raw profile hash identifies every research observation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path


class LabSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy_version: str
    flow_window_seconds: int = Field(gt=0)
    poll_max_age_seconds: int = Field(gt=0)
    min_directional_premium_usd: float = Field(gt=0)
    dominance_min: float = Field(gt=0.5, le=1)
    side_known_min: float = Field(ge=0, le=1)
    min_contracts: int = Field(ge=2)
    recovery_min_atr: float = Field(ge=0)
    chase_max_atr: float = Field(gt=0)
    spot_max_age_seconds: int = Field(gt=0)
    atr_period: int = Field(ge=1)
    atr_min_sessions: int = Field(ge=1)
    benchmark: str
    primary_sessions: int = Field(gt=0)
    secondary_sessions: tuple[int, ...]
    slippage_bps_per_side: float = Field(ge=0, lt=10000)
    news_window_hours: float = Field(gt=0)
    news_max_age_seconds: int = Field(gt=0)
    display_limit: int = Field(gt=0)
    outcome_batch_size: int = Field(gt=0)
    archive_timeout_seconds: float = Field(gt=0)
    cycle_timeout_seconds: float = Field(gt=0)
    profile_hash: str = ""

    @model_validator(mode="after")
    def check(self) -> LabSettings:
        if self.recovery_min_atr > self.chase_max_atr:
            raise ValueError("recovery exceeds chase limit")
        if self.primary_sessions in self.secondary_sessions or len(
            set(self.secondary_sessions)
        ) != len(self.secondary_sessions):
            raise ValueError("horizons must be distinct")
        if any(n < 1 for n in self.secondary_sessions):
            raise ValueError("invalid horizon")
        return self


def load_settings(path: Path | None = None) -> LabSettings:
    raw = (path or profile_path("alpha_lab_v1.yaml")).read_bytes()
    data = YAML(typ="safe").load(raw)
    return LabSettings(**data, profile_hash=hashlib.sha256(raw).hexdigest())
