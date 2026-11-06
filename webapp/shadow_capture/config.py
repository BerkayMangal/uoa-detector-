"""Registered operational limits and immutable source identities."""
from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.news_research import config as news_config

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / 'profiles/shadow_price_v1.yaml'


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    version: Literal['shadow_price_v1']
    enabled_env: Literal['SHADOW_CAPTURE_ENABLED']
    poll_seconds: Literal[5]
    idle_poll_seconds: Literal[60]
    source_max_age_seconds: Literal[60]
    source_row_limit: Literal[128]
    candidate_slots: Literal[4]
    entry_delay_seconds: Literal[60]
    horizons_minutes: tuple[Literal[15], Literal[60]]
    window_seconds: Literal[300]
    attempt_offsets_seconds: tuple[Literal[2], Literal[32], Literal[62]]
    attempt_spacing_seconds: Literal[30]
    trade_max_age_seconds: Literal[60]
    pair_skew_seconds: Literal[60]
    request_timeout_seconds: Literal[8]
    max_body_bytes: Literal[65536]
    module_daily_cap: Literal[96]
    rate_limit_cooldown_seconds: Literal[60]
    reference_usd: Literal[2500]
    slippage_bps_per_side: Literal[5]


@cache
def settings() -> Settings:
    return Settings(**YAML(typ='safe').load(PROFILE.read_bytes()))


def profile_hash() -> str:
    return hashlib.sha256(PROFILE.read_bytes()).hexdigest()


@cache
def implementation_hash() -> str:
    # Presentation-only changes do not restart the measurement cohort.
    own = sorted(p for p in Path(__file__).parent.glob('*.py') if p.name != 'view.py')
    linked = [ROOT/'docs/phase-5.64-shadow-price-capture-acceptance.md',
              ROOT/'webapp/timed_measurement/transport.py',
              ROOT/'webapp/timed_measurement/collector.py']
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode()+p.read_bytes() for p in own+linked)).hexdigest()


def identity() -> dict[str, str]:
    return {'version': settings().version, 'profile_hash': profile_hash(),
            'implementation_hash': implementation_hash(),
            'live_profile_hash': load_live_alpha_settings().profile_sha256,
            'news_profile_hash': news_config.profile_hash(),
            'news_implementation_hash': news_config.implementation_hash()}


def enabled() -> bool:
    return os.getenv(settings().enabled_env, 'true').strip().lower() in {'true','1','yes','on'}
