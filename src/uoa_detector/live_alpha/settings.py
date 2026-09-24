"""Typed reader for ``profiles/live_alpha_v1.yaml`` (D8: every threshold lives there).

Strict: unknown keys are refused, so a mistyped threshold name fails at load time
instead of silently falling back to a default. The sha256 of the raw file travels
with the settings, and every stored recommendation cites it.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
from datetime import date, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

_REPO = pathlib.Path(__file__).resolve().parents[3]


def profile_path(name: str) -> pathlib.Path:
    """``profiles/<name>`` relative to the working directory, as every other profile
    reader here resolves it (the app starts from the repo root on Railway); the
    source-tree location is the fallback for an editable checkout run elsewhere."""
    local = pathlib.Path("profiles") / name
    return local if local.exists() else _REPO / "profiles" / name


DEFAULT_PROFILE = profile_path("live_alpha_v1.yaml")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CalendarSettings(_Strict):
    timezone: str
    premarket_open: time
    regular_open: time
    regular_close: time
    early_close: time
    years: tuple[int, ...]
    holidays: tuple[date, ...]
    early_closes: tuple[date, ...]


class FlowSettings(_Strict):
    dedupe_seconds: int = Field(ge=0)
    min_directional_premium_usd: float = Field(gt=0)
    dominance_min: float = Field(gt=0.5, le=1)
    side_aware_share_min: float = Field(ge=0, le=1)
    min_distinct_contracts: int = Field(ge=1)


class PriceSettings(_Strict):
    spot_max_age_seconds: int = Field(gt=0)
    confirm_min_atr: float = Field(ge=0)
    chase_max_atr: float = Field(gt=0)
    relative_min: float = Field(ge=0)
    benchmark: str = Field(min_length=1)


class NewsSettings(_Strict):
    window_hours: float = Field(gt=0)
    refresh_seconds: int = Field(gt=0)
    limit: int = Field(ge=1, le=100)


class StockPlanSettings(_Strict):
    stop_atr: float = Field(gt=0)
    target_r: float = Field(gt=0)
    horizon_sessions: int = Field(ge=1)
    r_usd: float = Field(gt=0)
    max_notional_usd: float = Field(gt=0)
    slippage_bps: float = Field(ge=0)


class OptionPlanSettings(_Strict):
    min_dte: int = Field(ge=1)
    max_dte: int = Field(ge=1)
    quote_max_age_seconds: int = Field(gt=0)
    max_roundtrip_cost_pct: float = Field(gt=0)
    strike_grid_candidates: tuple[float, ...]
    max_symbols_per_request: int = Field(ge=2)


class PaperSettings(_Strict):
    enabled: bool


class CycleSettings(_Strict):
    live_seconds: int = Field(ge=60)
    closed_seconds: int = Field(ge=60)
    max_cards: int = Field(ge=1)


class LiveAlphaSettings(_Strict):
    policy_version: str = Field(min_length=1)
    calendar: CalendarSettings
    flow: FlowSettings
    price: PriceSettings
    news: NewsSettings
    stock_plan: StockPlanSettings
    option_plan: OptionPlanSettings
    paper: PaperSettings
    cycle: CycleSettings
    derived_from: tuple[str, ...]
    design_difference: str
    profile_sha256: str = ""

    @model_validator(mode="after")
    def _ranges(self) -> LiveAlphaSettings:
        if self.option_plan.min_dte > self.option_plan.max_dte:
            raise ValueError("option_plan.min_dte cannot exceed option_plan.max_dte")
        if not self.derived_from:
            raise ValueError("derived_from must name the rejected work this policy builds on")
        return self


def load_live_alpha_settings(path: pathlib.Path | str = DEFAULT_PROFILE) -> LiveAlphaSettings:
    file_path = pathlib.Path(path)
    raw = file_path.read_bytes()
    data: Any = YAML(typ="safe").load(io.BytesIO(raw))
    if not isinstance(data, dict):
        raise ValueError(f"{file_path}: profile is not a mapping")
    return LiveAlphaSettings(**data, profile_sha256=hashlib.sha256(raw).hexdigest())
