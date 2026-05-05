"""Phase 3.3.1 tests for ``Credentials`` (config.credentials).

Pins:
  - empty/missing env → load succeeds, all fields None (lazy validation)
  - require_* methods raise MissingCredentialError when field is None
  - error message names the exact env variable
  - SecretStr never reveals the secret in repr / str / model_dump
  - .env file loading works (tmp_path-isolated)
  - explicit-injection bypasses env loading
  - case-insensitive env loading (per SettingsConfigDict)
  - missing one credential doesn't poison errors for another
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from uoa_detector.config import (
    Credentials,
    MissingCredentialError,
    load_credentials,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no credential env vars leak from the host environment.

    Tests that explicitly want to test env-driven loading set vars
    via monkeypatch.setenv after this fixture runs.
    """
    for name in (
        "THETADATA_API_KEY", "THETADATA_USERNAME", "UNUSUAL_WHALES_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move CWD to a temp dir so .env at repo root doesn't bleed in."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Empty / missing — load succeeds, require_* raises
# ---------------------------------------------------------------------------


def test_load_with_empty_env_succeeds_with_all_none(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """Loading with no env vars and no .env file: every field is None."""
    creds = Credentials()
    assert creds.thetadata_api_key is None
    assert creds.thetadata_username is None
    assert creds.unusual_whales_api_key is None


def test_load_credentials_helper_equivalent_to_constructor(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """load_credentials() == Credentials() in default case."""
    a = Credentials()
    b = load_credentials()
    assert a == b


def test_require_thetadata_api_key_raises_when_missing(
    clean_env: None, isolated_workdir: Path,
) -> None:
    creds = Credentials()
    with pytest.raises(MissingCredentialError) as exc:
        creds.require_thetadata_api_key()
    msg = str(exc.value)
    assert "THETADATA_API_KEY" in msg
    assert "ThetaData" in msg
    assert ".env" in msg


def test_require_thetadata_username_raises_when_missing(
    clean_env: None, isolated_workdir: Path,
) -> None:
    creds = Credentials()
    with pytest.raises(MissingCredentialError) as exc:
        creds.require_thetadata_username()
    assert "THETADATA_USERNAME" in str(exc.value)


def test_require_uw_key_raises_when_missing(
    clean_env: None, isolated_workdir: Path,
) -> None:
    creds = Credentials()
    with pytest.raises(MissingCredentialError) as exc:
        creds.require_unusual_whales_api_key()
    assert "UNUSUAL_WHALES_API_KEY" in str(exc.value)
    assert "Unusual Whales" in str(exc.value)


def test_missing_one_does_not_poison_others(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only UW is set, require_uw works; require_thetadata raises."""
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw_test_value")
    creds = Credentials()
    # UW works
    uw = creds.require_unusual_whales_api_key()
    assert uw.get_secret_value() == "uw_test_value"
    # ThetaData raises
    with pytest.raises(MissingCredentialError):
        creds.require_thetadata_api_key()


# ---------------------------------------------------------------------------
# Env-driven loading
# ---------------------------------------------------------------------------


def test_loads_from_env_variables(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THETADATA_API_KEY", "td_env_value")
    monkeypatch.setenv("THETADATA_USERNAME", "td_user_env")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw_env_value")

    creds = Credentials()
    assert creds.thetadata_api_key is not None
    assert creds.thetadata_api_key.get_secret_value() == "td_env_value"
    assert creds.thetadata_username is not None
    assert creds.thetadata_username.get_secret_value() == "td_user_env"
    assert creds.unusual_whales_api_key is not None
    assert creds.unusual_whales_api_key.get_secret_value() == "uw_env_value"


def test_env_var_names_case_insensitive(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SettingsConfigDict has case_sensitive=False; lower-case env names work."""
    monkeypatch.setenv("thetadata_api_key", "td_lower")
    creds = Credentials()
    assert creds.thetadata_api_key is not None
    assert creds.thetadata_api_key.get_secret_value() == "td_lower"


# ---------------------------------------------------------------------------
# .env file loading
# ---------------------------------------------------------------------------


def test_loads_from_dotenv_file(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """When CWD has a .env, values come from it."""
    dotenv = isolated_workdir / ".env"
    dotenv.write_text(
        "THETADATA_API_KEY=td_dotenv_value\n"
        "UNUSUAL_WHALES_API_KEY=uw_dotenv_value\n",
        encoding="utf-8",
    )
    creds = Credentials()
    assert creds.thetadata_api_key is not None
    assert creds.thetadata_api_key.get_secret_value() == "td_dotenv_value"
    assert creds.unusual_whales_api_key is not None
    assert creds.unusual_whales_api_key.get_secret_value() == "uw_dotenv_value"
    # Username not in .env → None
    assert creds.thetadata_username is None


def test_env_var_overrides_dotenv(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var has higher priority than .env file (per pydantic-settings default)."""
    dotenv = isolated_workdir / ".env"
    dotenv.write_text("THETADATA_API_KEY=td_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("THETADATA_API_KEY", "td_env_wins")

    creds = Credentials()
    assert creds.thetadata_api_key is not None
    assert creds.thetadata_api_key.get_secret_value() == "td_env_wins"


# ---------------------------------------------------------------------------
# Constructor injection
# ---------------------------------------------------------------------------


def test_constructor_injection_bypasses_env(
    isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs win over env vars (used by tests)."""
    monkeypatch.setenv("THETADATA_API_KEY", "from_env")
    creds = Credentials(thetadata_api_key=SecretStr("from_kwarg"))
    assert creds.thetadata_api_key is not None
    assert creds.thetadata_api_key.get_secret_value() == "from_kwarg"


# ---------------------------------------------------------------------------
# Secret redaction — repr / str / model_dump never reveal the secret
# ---------------------------------------------------------------------------


def test_secretstr_never_in_repr(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """SecretStr's repr is '**********' regardless of the underlying value."""
    creds = Credentials(
        thetadata_api_key=SecretStr("td_secret_xyz"),
        unusual_whales_api_key=SecretStr("uw_secret_abc_def_123"),
    )
    rep = repr(creds)
    assert "td_secret_xyz" not in rep
    assert "uw_secret_abc_def_123" not in rep


def test_secretstr_never_in_str(
    clean_env: None, isolated_workdir: Path,
) -> None:
    creds = Credentials(thetadata_api_key=SecretStr("td_secret_xyz"))
    assert "td_secret_xyz" not in str(creds)


def test_model_dump_does_not_leak_secret(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """Pydantic's model_dump() preserves SecretStr objects (which redact);
    callers who want raw values must call .get_secret_value() explicitly.

    This test asserts the dump representation never contains the literal
    secret string.
    """
    creds = Credentials(thetadata_api_key=SecretStr("td_secret_xyz"))
    dumped: Any = creds.model_dump()
    # The dumped value is a SecretStr instance, not the literal string;
    # str() of that is the redaction marker.
    flat = repr(dumped)
    assert "td_secret_xyz" not in flat


def test_model_dump_json_does_not_leak_secret(
    clean_env: None, isolated_workdir: Path,
) -> None:
    """JSON serialization redacts SecretStr to '**********'."""
    creds = Credentials(thetadata_api_key=SecretStr("td_secret_xyz"))
    js = creds.model_dump_json()
    assert "td_secret_xyz" not in js


# ---------------------------------------------------------------------------
# require_* returns SecretStr (not the raw string)
# ---------------------------------------------------------------------------


def test_require_returns_secretstr_not_raw(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_thetadata_api_key returns SecretStr; caller must
    .get_secret_value() at the request site."""
    monkeypatch.setenv("THETADATA_API_KEY", "td_test")
    creds = Credentials()
    secret = creds.require_thetadata_api_key()
    assert isinstance(secret, SecretStr)
    # Repr is redacted
    assert "td_test" not in repr(secret)
    # Explicit unwrap returns the value
    assert secret.get_secret_value() == "td_test"


# ---------------------------------------------------------------------------
# MissingCredentialError doesn't leak other credentials
# ---------------------------------------------------------------------------


def test_missing_error_does_not_mention_other_credentials(
    clean_env: None, isolated_workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If UW is set but ThetaData isn't, the error for ThetaData
    must not include the UW value or even its env-var name."""
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw_secret_value_xyz")
    creds = Credentials()
    with pytest.raises(MissingCredentialError) as exc:
        creds.require_thetadata_api_key()
    msg = str(exc.value)
    assert "uw_secret_value_xyz" not in msg
    assert "UNUSUAL_WHALES_API_KEY" not in msg
