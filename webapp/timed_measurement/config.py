"""Fixed operational capture limits; no live strategy or research override."""
from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy.engine import Engine, make_url

from webapp.board.db import make_engine, normalize_database_url

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles/timed_measurement_v1.yaml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["timed_measurement_v1"]
    enabled_env: Literal["TIMED_MEASUREMENT_ENABLED"]
    source_path: Literal["/api/stock/{ticker}/quote"]
    idle_poll_seconds: Literal[15]
    active_poll_seconds: float = Field(gt=0, le=5)
    attempt_offsets_seconds: tuple[Literal[2], Literal[32], Literal[62]]
    attempt_spacing_seconds: Literal[30]
    max_attempts: Literal[3]
    trade_max_age_seconds: Literal[60]
    pair_skew_seconds: Literal[60]
    request_timeout_seconds: Literal[8]
    max_body_bytes: Literal[65536]
    module_daily_cap: Literal[256]
    read_limit: Literal[200]
    slot_conflict_retries: Literal[8]
    statement_timeout_ms: Literal[2000]
    lock_timeout_ms: Literal[1000]
    rate_limit_cooldown_seconds: Literal[60]


@cache
def settings() -> Settings:
    return Settings(**YAML(typ="safe").load(PROFILE.read_bytes()))


def enabled() -> bool:
    return os.environ.get(settings().enabled_env, "").lower().strip() in {"1", "true", "yes"}


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


def configured_engine(database_url: str) -> Engine:
    url = make_url(normalize_database_url(database_url))
    if url.get_backend_name() == "postgresql":
        original = url.query.get("options", os.environ.get("PGOPTIONS", ""))
        if not isinstance(original, str):
            raise ValueError("ambiguous database options")
        cfg = settings()
        options = f"{original} -c statement_timeout={cfg.statement_timeout_ms} -c lock_timeout={cfg.lock_timeout_ms}".strip()
        url = url.update_query_dict({"options": options})
    return make_engine(url.render_as_string(hide_password=False))
