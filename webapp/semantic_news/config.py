"""Frozen inference bounds; the private credential is never a profile value."""

from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/semantic_news_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["semantic_news_v1"]
    enabled_env: Literal["SEMANTIC_NEWS_ENABLED"]
    endpoint: Literal["https://mahalle-production.up.railway.app/internal/uoa/news-analyze"]
    token_env: Literal["MAHALLE_ANALYSIS_TOKEN"]
    poll_seconds: Literal[2]
    idle_poll_seconds: Literal[60]
    request_timeout_seconds: Literal[45]
    max_input_bytes: Literal[16384]
    max_body_bytes: Literal[65536]
    max_items: Literal[3]
    max_headline_characters: Literal[1600]
    daily_analyses: Literal[4]
    min_completion_lead_seconds: Literal[5]
    news_window_hours: Literal[36]
    source_row_limit: Literal[128]
    expected_prompt_hash: Literal[
        "b351f68d8dd3b7c95605a1506697829317e91ae980e7ec4814c054bfa378b0f1"
    ]
    expected_schema_hash: Literal[
        "5ffd00f86a39e598a048f21f59b63b2c676155b1ece90fe262c1c56d95c51d1c"
    ]


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
    return os.getenv(settings().enabled_env, "false").strip().lower() in {"true", "yes", "1", "on"}
