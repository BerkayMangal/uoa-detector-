"""Application settings.

In Phase 2 the v5 numeric values moved into ``CalibrationProfile`` (loaded from
``profiles/*.yaml``). What remains here is environment-driven runtime config:
where to look for profile files, default profile filename, log level, etc.

Kept as a Pydantic ``BaseSettings`` so values can be overridden via env vars
prefixed ``UOA_`` — useful in CI and Phase-3 deployments.

Phase 3.3.1: ``Credentials`` model added in ``config.credentials`` for API
keys (ThetaData, Unusual Whales). Re-exported here for convenience but the
two models are intentionally independent — different env-var prefixes,
different lifecycles.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from uoa_detector.config.credentials import (
    Credentials,
    MissingCredentialError,
    load_credentials,
)


class AppSettings(BaseSettings):
    """Runtime settings — paths, log level. Numeric tunables live in profiles."""

    model_config = SettingsConfigDict(
        env_prefix="UOA_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    profiles_dir: Path = Path("profiles")
    default_profile_filename: str = "v5_default.yaml"
    log_level: str = "INFO"


def default_settings() -> AppSettings:
    """Return runtime settings; respects ``UOA_*`` environment overrides."""
    return AppSettings()


__all__ = [
    "AppSettings",
    "Credentials",
    "MissingCredentialError",
    "default_settings",
    "load_credentials",
]
