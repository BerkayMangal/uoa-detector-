"""Fixed, separate curated-context transport; never a live signal profile."""
from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

Provider = Literal["mahalle", "stajyer"]
ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/shared_news_v1.yaml"
ALLOWLIST = {
    "mahalle": ("https://mahalle-production.up.railway.app/news-feed.json", "MAHALLE_NEWS_FEED_TOKEN", "curated_digest"),
    "stajyer": ("https://stajyer-production.up.railway.app/api/news", "STAJYER_NEWS_FEED_TOKEN", "curated_note_sources"),
}


class Endpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Provider
    url: str
    token_env: str
    coverage: str

    @model_validator(mode="after")
    def exact_endpoint(self) -> Endpoint:
        if (self.url, self.token_env, self.coverage) != ALLOWLIST[self.provider]:
            raise ValueError("only the dedicated exact provider endpoint is allowed")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["shared_news_v1"]
    enabled_env: Literal["SHARED_NEWS_ENABLED"]
    poll_seconds: int = Field(ge=900)
    request_timeout_seconds: float = Field(gt=0, le=10)
    recording_timeout_seconds: float = Field(gt=0, le=10)
    max_body_bytes: int = Field(gt=0, le=1048576)
    max_items: int = Field(gt=0, le=100)
    max_age_seconds: int = Field(gt=0)
    read_packet_limit: int = Field(gt=0, le=100)
    providers: tuple[Endpoint, ...]

    @model_validator(mode="after")
    def two_providers(self) -> Settings:
        if tuple(p.provider for p in self.providers) != ("mahalle", "stajyer"):
            raise ValueError("exactly two distinct ordered providers required")
        return self


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_text()))


def enabled() -> bool:
    return os.environ.get(settings().enabled_env, "").strip().lower() in {"1", "true", "yes"}


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()
