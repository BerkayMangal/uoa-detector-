"""Frozen independent news-research protocol; no live policy override."""
from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/news_research_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    entry_delay_minutes: int = Field(gt=0)
    holding_minutes: int = Field(gt=0)
    mark_tolerance_minutes: int = Field(gt=0)
    max_units_per_day: int = Field(gt=0)
    notional_usd: float = Field(gt=0)
    slippage_bps_per_side: float = Field(ge=0, lt=10000)
    stress_slippage_bps_per_side: tuple[float, ...]
    minimum_common_sessions: int = Field(gt=0)
    news_window_hours: float = Field(gt=0)
    news_max_age_seconds: int = Field(gt=0)
    source_max_age_seconds: int = Field(gt=0)
    flow_max_age_seconds: int = Field(gt=0)
    row_limit: int = Field(gt=0)
    recording_timeout_seconds: float = Field(gt=0)


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_text()))


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


def implementation_hash() -> str:
    files = sorted(Path(__file__).parent.glob("*.py"))
    contract = ROOT / "docs/phase-5.53-news-evidence-acceptance.md"
    return hashlib.sha256(
        b"".join(p.name.encode() + p.read_bytes() for p in files)
        + contract.name.encode() + contract.read_bytes()
    ).hexdigest()
