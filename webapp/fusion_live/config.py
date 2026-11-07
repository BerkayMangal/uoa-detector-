"""Fixed forward arm definitions and implementation identity."""

from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/fusion_live_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["fusion_live_v1"]
    enabled_env: Literal["FUSION_ENGINE_ENABLED"]
    poll_seconds: Literal[2]
    idle_poll_seconds: Literal[60]
    candidate_limit: Literal[4]
    entry_lead_seconds: Literal[5]
    read_limit: Literal[32]
    history_read_limit: Literal[128]
    arms: tuple[
        Literal["base"],
        Literal["semantic_positive"],
        Literal["semantic_veto"],
        Literal["semantic_fundamental"],
    ]
    fundamental_revenue_yoy_min_pct: Literal[0]
    fundamental_operating_margin_min_pct: Literal[0]
    minimum_complete_sessions: Literal[80]


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_bytes()))


def enabled() -> bool:
    return os.getenv(settings().enabled_env, "true").strip().lower() in {"true", "1", "yes", "on"}


def identity() -> dict[str, str]:
    files = sorted(p for p in Path(__file__).parent.glob("*.py") if p.name != "view.py")
    for component in ("semantic_news", "fundamental_context"):
        files += sorted(
            p for p in (ROOT / "webapp" / component).glob("*.py") if p.name != "view.py"
        )
    files += [
        ROOT / "webapp/evidence_reads.py",
        ROOT / "profiles/semantic_news_v1.yaml",
        ROOT / "profiles/fundamental_context_v1.yaml",
    ]
    from webapp.shadow_capture import config as source_config
    from webapp.shadow_capture.model import canonical

    content = b"".join(
        str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in files
    ) + canonical(source_config.identity())
    return {
        "version": settings().version,
        "profile_hash": hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
        "implementation_hash": hashlib.sha256(content).hexdigest(),
    }
