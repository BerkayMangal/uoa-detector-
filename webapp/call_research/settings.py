from __future__ import annotations

import hashlib
from datetime import date
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/call_research_v1.yaml"


class Window(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    start: date
    end: date


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    source_path: str
    source_sha256: str
    benchmarks: tuple[str, str]
    reference_only: tuple[str, ...]
    relative_min: float = Field(ge=0)
    persistence_sessions: int = Field(gt=0)
    retest_delay_sessions: int = Field(gt=0)
    capital_usd: float = Field(gt=0)
    position_usd: float = Field(gt=0)
    max_positions: int = Field(gt=0)
    primary_horizon: int = Field(gt=0)
    horizons: tuple[int, ...]
    slippage_bps: tuple[float, ...]
    windows: tuple[Window, ...]
    arms: tuple[str, ...]
    remove_best_wins: tuple[int, ...]
    bootstrap_reps: int = Field(gt=0)
    bootstrap_block_sessions: int = Field(gt=0)
    bootstrap_seed: int
    bootstrap_quantiles: tuple[float, float]
    prospective_start: date
    prospective_extra_symbols: tuple[str, ...]
    context_tickers: tuple[str, ...]
    capture_max_symbols: int = Field(gt=0)
    observer_seconds: int = Field(gt=0)
    observer_timeout_seconds: int = Field(gt=0)
    query_limit: int = Field(gt=0)
    display_limit: int = Field(gt=0)


@cache
def settings() -> Settings:
    return Settings.model_validate(YAML(typ="safe").load(PROFILE.read_bytes()))


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()
