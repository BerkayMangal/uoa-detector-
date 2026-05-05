"""Phase 3.3.1.4 — verify the pre-commit hook catches credential leaks.

Per acceptance doc: 'Pre-commit hook catches a fake uw_ key
inserted into a staged file (verified by a CI test that
intentionally stages a bad string and asserts the hook fails)'.

Tests use a temp git repo to avoid modifying the real one. They
copy the actual hook script into the temp repo and exercise its
two modes:

  - Default mode: scans staged diff (the pre-commit usage)
  - --check-tree mode: scans all tracked files (the CI usage)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_SCRIPT = REPO_ROOT / "scripts" / "check_no_secrets.sh"


def _run_in(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Initialise a temp git repo with the hook copied in."""
    _run_in(tmp_path, "git", "init", "-q")
    _run_in(tmp_path, "git", "config", "user.email", "t@t.local")
    _run_in(tmp_path, "git", "config", "user.name", "test")
    # Copy hook script into the temp repo
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(HOOK_SCRIPT, scripts_dir / "check_no_secrets.sh")
    (scripts_dir / "check_no_secrets.sh").chmod(0o755)
    # Initial commit so 'git diff --cached' has a baseline
    (tmp_path / "README.md").write_text("# test repo\n", encoding="utf-8")
    _run_in(tmp_path, "git", "add", "README.md", "scripts/")
    _run_in(tmp_path, "git", "commit", "-q", "-m", "initial")
    return tmp_path


# ---------------------------------------------------------------------------
# Hook script presence + executability
# ---------------------------------------------------------------------------


def test_hook_script_exists() -> None:
    assert HOOK_SCRIPT.exists()


def test_hook_script_is_executable() -> None:
    import os
    assert os.access(HOOK_SCRIPT, os.X_OK), (
        f"{HOOK_SCRIPT} is not executable. Run "
        "'chmod +x scripts/check_no_secrets.sh' and re-commit."
    )


# ---------------------------------------------------------------------------
# Staged-diff mode (the pre-commit usage)
# ---------------------------------------------------------------------------


def test_hook_passes_clean_staged_diff(temp_git_repo: Path) -> None:
    """Clean diff → exit 0."""
    (temp_git_repo / "src.py").write_text(
        "def hello(): return 'world'\n", encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", "src.py")
    result = _run_in(temp_git_repo, "bash", "scripts/check_no_secrets.sh")
    assert result.returncode == 0, (
        f"Clean diff was flagged. stderr:\n{result.stderr}"
    )


def test_hook_catches_uw_api_key_in_staged_diff(temp_git_repo: Path) -> None:
    """A fake uw_<32 hex> key in staged content → exit 1."""
    (temp_git_repo / "leak.py").write_text(
        # An exact uw_ + 32 hex char string — the canonical UW format.
        'API_KEY = "uw_0123456789abcdef0123456789abcdef"\n',
        encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", "leak.py")
    result = _run_in(temp_git_repo, "bash", "scripts/check_no_secrets.sh")
    assert result.returncode == 1, (
        f"Hook missed UW key. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "credential leak detected" in result.stderr


def test_hook_catches_api_key_assignment(temp_git_repo: Path) -> None:
    """Generic api_key=<long-value> assignment → exit 1."""
    (temp_git_repo / "leak.py").write_text(
        'api_key = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"\n',
        encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", "leak.py")
    result = _run_in(temp_git_repo, "bash", "scripts/check_no_secrets.sh")
    assert result.returncode == 1


def test_hook_passes_short_api_key_value(temp_git_repo: Path) -> None:
    """Short value (< 20 chars) doesn't trip — likely not a real key."""
    (temp_git_repo / "fine.py").write_text(
        'api_key = "short"\n',  # too short to be a real key
        encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", "fine.py")
    result = _run_in(temp_git_repo, "bash", "scripts/check_no_secrets.sh")
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# --check-tree mode (the CI usage)
# ---------------------------------------------------------------------------


def test_hook_check_tree_passes_on_clean_repo(temp_git_repo: Path) -> None:
    """A clean tree (only README + the hook itself) → exit 0."""
    result = _run_in(
        temp_git_repo, "bash", "scripts/check_no_secrets.sh", "--check-tree",
    )
    assert result.returncode == 0


def test_hook_check_tree_catches_committed_uw_key(temp_git_repo: Path) -> None:
    """A committed file with a uw_ key → exit 1 in --check-tree mode."""
    (temp_git_repo / "config.py").write_text(
        'UW_KEY = "uw_0123456789abcdef0123456789abcdef"\n',
        encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", "config.py")
    _run_in(temp_git_repo, "git", "commit", "-q", "-m", "add config")

    result = _run_in(
        temp_git_repo, "bash", "scripts/check_no_secrets.sh", "--check-tree",
    )
    assert result.returncode == 1
    assert "credential leak" in result.stderr


def test_hook_check_tree_skips_self_and_env_example(
    temp_git_repo: Path,
) -> None:
    """The hook itself and .env.example are explicit allowlist entries.

    Without this, the hook would flag its own pattern strings.
    """
    # Add an .env.example with key NAMES (not values).
    (temp_git_repo / ".env.example").write_text(
        "UNUSUAL_WHALES_API_KEY=\nTHETADATA_API_KEY=\n",
        encoding="utf-8",
    )
    _run_in(temp_git_repo, "git", "add", ".env.example")
    _run_in(temp_git_repo, "git", "commit", "-q", "-m", "add example")

    result = _run_in(
        temp_git_repo, "bash", "scripts/check_no_secrets.sh", "--check-tree",
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# CI hook check on the real repo
# ---------------------------------------------------------------------------


def test_real_repo_check_tree_passes() -> None:
    """The real repo, scanned with --check-tree, must be clean.

    This is the always-on guard: if anyone commits a credential into
    the repo, this test fails on the next pytest run regardless of
    whether the pre-commit hook was installed.
    """
    result = _run_in(
        REPO_ROOT, "bash", "scripts/check_no_secrets.sh", "--check-tree",
    )
    assert result.returncode == 0, (
        f"CI hook check FAILED on real repo. stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
        "If this is a false positive, update the hook's allowlist; "
        "if it's a real leak, rotate the credential and rewrite history."
    )
