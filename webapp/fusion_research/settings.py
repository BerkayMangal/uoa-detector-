"""The complete search/split budget is frozen before any returns are read."""

from __future__ import annotations

import hashlib
from datetime import date
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/fusion_research_v1.yaml"


class Fold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    train_end: date
    validation_start: date
    validation_end: date


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["fusion_research_v1"]
    source_start: date
    source_end: date
    families: dict[str, tuple[float, ...]]
    lookbacks: tuple[int, ...]
    volume_gates: tuple[float, ...]
    regimes: tuple[str, ...]
    directions: tuple[Literal["up", "down"], ...]
    horizons: tuple[int, ...]
    registered_configurations: int = Field(gt=0, le=8192)
    configuration_cap: Literal[8192]
    max_run_seconds: Literal[900]
    atr_sessions: int = Field(gt=0)
    volume_sessions: int = Field(gt=0)
    market_ma_sessions: int = Field(gt=0)
    compression_fast_sessions: int = Field(gt=0)
    compression_slow_sessions: int = Field(gt=0)
    recovery_chase_atr: float = Field(gt=0)
    breakout_chase_atr: float = Field(gt=0)
    reference_usd: float = Field(gt=0)
    risk_usd: float = Field(gt=0)
    risk_atr_multiple: float = Field(gt=0)
    max_new_units_per_day: int = Field(gt=0)
    cost_bps_per_side: float = Field(ge=0, lt=10000)
    stress_bps_per_side: tuple[float, ...]
    entry_delay_sessions: Literal[1]
    purge_sessions: int = Field(ge=5)
    embargo_sessions: int = Field(ge=1)
    folds: tuple[Fold, ...]
    final_train_end: date
    final_start: date
    final_end: date
    minimum_train_units: Literal[60]
    minimum_train_days: Literal[20]
    minimum_validation_units: Literal[20]
    minimum_validation_days: Literal[10]
    ticker_shrinkage_units: Literal[60]
    minimum_prospective_sessions: Literal[80]
    bootstrap_draws: Literal[200]
    bootstrap_block_sessions: Literal[5]
    bootstrap_seed: int
    bootstrap_percentile: Literal[95]
    report_max_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def valid(self) -> Settings:
        axes = (
            self.lookbacks,
            self.volume_gates,
            self.regimes,
            self.directions,
            self.horizons,
            *self.families.values(),
        )
        if any(len(set(v)) != len(v) for v in axes):
            raise ValueError("duplicate parameter values are not independent configurations")
        n = (
            sum(len(v) for v in self.families.values())
            * len(self.lookbacks)
            * len(self.volume_gates)
            * len(self.regimes)
            * len(self.directions)
            * len(self.horizons)
        )
        if (
            n != self.registered_configurations
            or n > self.configuration_cap
            or self.purge_sessions < max(self.horizons)
        ):
            raise ValueError("unregistered grid or insufficient purge")
        if any(
            not (
                self.source_start
                < f.train_end
                < f.validation_start
                <= f.validation_end
                < self.final_start
            )
            for f in self.folds
        ):
            raise ValueError("invalid temporal split")
        if not self.final_train_end < self.final_start <= self.final_end <= self.source_end:
            raise ValueError("invalid final split")
        return self


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_text()))


def identity() -> dict[str, str]:
    files = sorted(Path(__file__).parent.glob("*.py")) + [
        ROOT / p
        for p in (
            "src/uoa_detector/live_alpha/calendar.py",
            "src/uoa_detector/live_alpha/settings.py",
            "profiles/live_alpha_v1.yaml",
            "webapp/calibration/settings.py",
            "profiles/calibration_v1.yaml",
            "docs/phase-5.66-fusion-engine-acceptance.md",
        )
    ]
    return {
        "profile_hash": hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
        "code_hash": hashlib.sha256(
            b"".join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in files)
        ).hexdigest(),
    }
