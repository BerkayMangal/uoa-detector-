"""Pinned operational bounds; original strategy/capture profiles are unchanged."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/daily_guardian_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["daily_guardian_v1"]
    enabled_env: Literal["DAILY_DATA_GUARDIAN_ENABLED"]
    start_et: Literal["05:00"]
    stop_et: Literal["09:15"]
    poll_seconds: Literal[60]
    idle_poll_seconds: Literal[300]
    owner_read_limit: Literal[256]
    max_result_bytes: Literal[262144]
    history_sessions: Literal[60]
    max_requests: Literal[82]
    quota_reserve: Literal[5000]
    spacing_seconds: float = Field(ge=0.75, le=0.75)
    timeout_seconds: Literal[30]
    max_packet_rows: Literal[3000]


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_bytes()))


def enabled() -> bool:
    return os.getenv("DAILY_DATA_GUARDIAN_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
