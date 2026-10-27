"""Separate operational limits for application database sessions."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_parallel_workers_per_gather: Literal[0]


@lru_cache(maxsize=1)
def settings() -> DatabaseSettings:
    return DatabaseSettings(**YAML(typ="safe").load(profile_path("database_v1.yaml").read_text()))
