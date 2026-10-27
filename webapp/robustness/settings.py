from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: str
    entry_delays: tuple[Annotated[int, Field(ge=0)], ...]
    slippage_bps: tuple[Annotated[float, Field(ge=0, lt=10000)], ...]
    remove_best_trades: tuple[Annotated[int, Field(gt=0)], ...]
    profile_hash: str = Field(min_length=64, max_length=64)


@lru_cache(maxsize=1)
def settings() -> Settings:
    raw = profile_path("stress_v1.yaml").read_bytes()
    return Settings(**YAML(typ="safe").load(raw), profile_hash=hashlib.sha256(raw).hexdigest())
