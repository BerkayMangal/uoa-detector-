"""Phase 3.3.1.3 — verify ``.env.example`` is committed and blank.

The acceptance doc requires that ``.env.example`` is committed at
repo root with all credential keys present but values blank, so
contributors know what env variables exist without leaking real
credentials. This test pins both contracts:

  1. The file exists at repo root.
  2. Every key has an empty value (``KEY=`` not ``KEY=actual_value``).
  3. All three credentials are mentioned by name.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"

REQUIRED_KEYS = (
    "THETADATA_API_KEY",
    "THETADATA_USERNAME",
    "UNUSUAL_WHALES_API_KEY",
)


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.exists(), (
        f".env.example missing at {ENV_EXAMPLE}. The Phase 3.3 "
        "acceptance doc requires this file at repo root with all "
        "credential keys present but values blank."
    )


def test_env_example_contains_all_required_keys() -> None:
    """Every required env variable name appears in .env.example."""
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in REQUIRED_KEYS:
        assert key in content, (
            f"{key} missing from .env.example. Add a line "
            f"'{key}=' (with no value) so contributors know it's expected."
        )


def test_env_example_values_are_blank() -> None:
    """Every credential key has an empty value (KEY= not KEY=value).

    Catches accidental commits of real credentials into the example
    file. The pre-commit hook in 3.3.1.4 is the second layer of
    defense; this is the first.
    """
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Lines look like KEY=value (no spaces around =, by .env convention).
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if any(key == required for required in REQUIRED_KEYS):
            assert value == "", (
                f".env.example: {key!r} has a non-empty value {value!r}. "
                "Template values must be blank; real values go in the "
                "(uncommitted) .env file."
            )


def test_gitignore_includes_dotenv() -> None:
    """Verify ``.env`` is in .gitignore (acceptance doc explicit check)."""
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.exists()
    lines = [ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()]
    assert ".env" in lines, (
        f".env not in .gitignore at {gitignore}. Add the line '.env' "
        "verbatim (not '.env*' or '*.env' which catch unintended files)."
    )
