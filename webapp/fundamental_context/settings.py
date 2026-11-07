"""Registered operational limits and independent fundamental identity."""

from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/fundamental_context_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["fundamental_context_v1"]
    enabled_env: Literal["FUNDAMENTAL_CONTEXT_ENABLED"]
    statement_endpoint: Literal["financials"]
    max_tickers_per_day: Literal[4]
    max_requests_per_day: Literal[8]
    request_timeout_seconds: Literal[8]
    max_body_bytes: Literal[1048576]
    poll_seconds: Literal[5]
    idle_poll_seconds: Literal[60]
    source_row_limit: Literal[256]
    eps_denominator_floor: float = Field(gt=0)
    read_limit: Literal[16]
    candidate_max_age_seconds: Literal[60]
    claim_pending_seconds: Literal[30]
    wire_min_spacing_seconds: Literal[1]


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_bytes()))


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


@cache
def implementation_hash() -> str:
    paths = sorted(p for p in Path(__file__).parent.glob("*.py") if p.name != "view.py")
    paths.append(ROOT / "webapp/evidence_reads.py")
    return hashlib.sha256(
        b"".join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in paths)
    ).hexdigest()


def identity() -> dict[str, str]:
    return {
        "version": settings().version,
        "profile_hash": profile_hash(),
        "implementation_hash": implementation_hash(),
    }


def enabled() -> bool:
    return os.getenv(settings().enabled_env, "true").strip().lower() in {"1", "true", "yes", "on"}
