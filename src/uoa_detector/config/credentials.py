"""Credential management — Phase 3.3.1.

Loads API credentials for ThetaData and Unusual Whales from
environment variables (with ``.env`` file support via
``pydantic-settings``). Credentials are wrapped in ``SecretStr`` so
the actual key never appears in ``repr()``, model dumps, or
structlog output (when paired with the ``redact_secrets`` log
processor).

decision (lazy validation, not at startup):
  All three credential fields are ``SecretStr | None``. Loading
  ``Credentials()`` never raises — even with no ``.env`` file and
  no env vars set, it succeeds with all fields ``None``. The
  caller (an adapter constructor) requests a specific credential
  via ``require_thetadata_api_key()`` etc., and that call raises
  ``MissingCredentialError`` with a clear message naming the env
  variable. This matches the acceptance doc:
  "Missing optional credentials → clear error message naming
  exactly which env variable is missing, when an adapter that
  needs it is instantiated (lazy, not at startup)".

decision (SecretStr at the boundary, never .get_secret_value()
in code outside HTTP request sites):
  The acceptance doc requires that ``.get_secret_value()`` is
  called only at the HTTP request site, never in logs or
  intermediate transforms. Adapters take the ``SecretStr`` directly
  via constructor injection; they call ``.get_secret_value()`` in
  the request-building method only. This keeps the leak surface
  minimal and auditable by grep.

decision (no nested env config):
  ``AppSettings`` uses ``env_prefix='UOA_'``. ``Credentials`` does
  NOT — env vars are bare ``THETADATA_API_KEY`` etc., per
  acceptance doc. Two distinct settings models, two distinct env
  conventions; ``Credentials`` matches the names that vendor docs
  use in their setup instructions, ``AppSettings`` matches the
  ``UOA_*`` runtime convention.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingCredentialError(RuntimeError):
    """Raised when an adapter requests a credential that wasn't loaded.

    Message names the exact env variable so the operator knows what
    to add to their ``.env``. Never includes the value of any other
    credential, even if loaded.
    """


_ENV_FILE: Final = Path(".env")


class Credentials(BaseSettings):
    """API credentials for external data sources.

    Fields are ``SecretStr | None``. Construction never raises on a
    missing field; per-credential validation happens at request time
    via ``require_*`` methods.

    Construction reads from (in priority order):
      1. constructor kwargs (used by tests for explicit injection)
      2. environment variables (``THETADATA_API_KEY`` etc.)
      3. ``.env`` file at the working directory
      4. defaults (all fields ``None``)

    Tests that need a clean slate pass ``_env_file=None`` and
    ``thetadata_api_key=None`` etc. explicitly; production usage just
    calls ``Credentials()``.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        # No prefix: env names are bare (THETADATA_API_KEY, not UOA_THETADATA_API_KEY).
        case_sensitive=False,
        extra="ignore",
    )

    thetadata_api_key: SecretStr | None = None
    thetadata_username: SecretStr | None = None
    unusual_whales_api_key: SecretStr | None = None

    # ---- Required-credential accessors ------------------------------

    def require_thetadata_api_key(self) -> SecretStr:
        """Return the ThetaData API key or raise with a clear message."""
        if self.thetadata_api_key is None:
            raise _missing("THETADATA_API_KEY", "ThetaData API key")
        return self.thetadata_api_key

    def require_thetadata_username(self) -> SecretStr:
        """Return the ThetaData username or raise with a clear message."""
        if self.thetadata_username is None:
            raise _missing("THETADATA_USERNAME", "ThetaData username")
        return self.thetadata_username

    def require_unusual_whales_api_key(self) -> SecretStr:
        """Return the Unusual Whales API key or raise with a clear message."""
        if self.unusual_whales_api_key is None:
            raise _missing(
                "UNUSUAL_WHALES_API_KEY",
                "Unusual Whales API key",
            )
        return self.unusual_whales_api_key


def _missing(env_var: str, human_name: str) -> MissingCredentialError:
    return MissingCredentialError(
        f"{human_name} is not configured. Set the {env_var} environment "
        f"variable, or add it to your .env file at the repo root. See "
        f".env.example for the full list of expected variables.",
    )


def load_credentials() -> Credentials:
    """Load ``Credentials`` from env + ``.env`` file.

    The default ``Credentials()`` constructor already does this; this
    helper exists so adapter code can write
    ``creds = load_credentials()`` for readability rather than the
    bare class call. Both forms are equivalent.
    """
    return Credentials()
