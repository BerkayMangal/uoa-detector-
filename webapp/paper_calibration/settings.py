"""Frozen settings for the prospective stock calibration cohort."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from ruamel.yaml import YAML

from webapp.paper_calibration.model import SOURCE_STOP_ATR, Arm, Rules

_PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "paper_calibration_v1.yaml"


class Settings(Rules):
    version: str = Field(min_length=1)
    source_policy: str = Field(min_length=1)
    source_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizon_sessions: int = Field(gt=0)
    benchmark: str = Field(min_length=1)
    markout_minutes: tuple[int, ...]
    markout_grace_seconds: int = Field(gt=0)
    markout_pair_skew_seconds: int = Field(gt=0)
    cycle_timeout_seconds: float = Field(gt=0)
    recovery_batch: int = Field(gt=0)
    arms: tuple[Arm, ...]
    profile_sha256: str

    @property
    def study_key(self) -> str:
        return f"{self.version}:{self.profile_sha256}"

    @model_validator(mode="after")
    def validate_comparison(self) -> Settings:
        if len({arm.key for arm in self.arms}) != len(self.arms):
            raise ValueError("duplicate PAPER calibration arm")
        if sum(not arm.confirm_entry for arm in self.arms) != 1:
            raise ValueError("one entry control is required")
        if not any(arm.stop_atr == SOURCE_STOP_ATR and arm.confirm_entry for arm in self.arms):
            raise ValueError("confirmed original-width control is required")
        if any(arm.stop_atr != SOURCE_STOP_ATR for arm in self.arms if not arm.confirm_entry):
            raise ValueError("the entry control must use the original stop")
        if (not self.markout_minutes or any(minutes <= 0 for minutes in self.markout_minutes)
                or tuple(sorted(set(self.markout_minutes))) != self.markout_minutes):
            raise ValueError("markout horizons must be positive and strictly increasing")
        return self


@lru_cache(maxsize=1)
def settings() -> Settings:
    raw = _PROFILE.read_bytes()
    return Settings(**YAML(typ="safe").load(raw), profile_sha256=hashlib.sha256(raw).hexdigest())
