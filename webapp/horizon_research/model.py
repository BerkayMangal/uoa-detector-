"""Typed immutable inputs for a reused-history sensitivity study."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/horizon_research_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    arms: tuple[str, ...]
    entry_delays_minutes: tuple[int, ...]
    bar_filters_minutes: tuple[int, ...]
    intraday_horizons_minutes: tuple[int, ...]
    session_close_offsets: tuple[int, ...]
    costs_bps_per_side: tuple[int, ...]
    entry_tolerance_minutes: int
    assumed_bar_lag_minutes: int
    unit_budget_usd: float
    primary_arm: str
    primary_delay_minutes: int
    primary_filter_minutes: int
    primary_cost_bps_per_side: int
    anchor_horizon: str
    bootstrap_draws: int
    bootstrap_block_sessions: int
    bootstrap_seed: int
    registered_cells: int
    source_start: date
    source_end: date
    source_sessions: int
    evaluation_sessions: int
    warmup_sessions: int


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_text()))


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


def code_hash() -> str:
    files = sorted(Path(__file__).parent.glob("*.py"))
    contracts = (
        ROOT / "docs/phase-5.54-horizon-study-acceptance.md",
        ROOT / "docs/phase-5.54-volume-quality-addendum.md",
    )
    return hashlib.sha256(
        b"".join(p.name.encode() + p.read_bytes() for p in files)
        + b"".join(p.name.encode() + p.read_bytes() for p in contracts)
    ).hexdigest()


@dataclass(frozen=True)
class Candle:
    ticker: str
    day: date
    at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class Signal:
    key: str
    arm: str
    ticker: str
    day: date
    at: datetime


@dataclass(frozen=True)
class Source:
    sessions: tuple[date, ...]
    evaluation_sessions: tuple[date, ...]
    signals: tuple[Signal, ...]
    candles: tuple[Candle, ...]
    source_hash: str
    original_report_hash: str
    packet_hashes: tuple[tuple[str, str], ...]
    quality: tuple[tuple[str, int], ...] = ()
