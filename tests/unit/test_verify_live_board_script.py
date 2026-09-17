"""The deploy rail itself (``scripts/verify_live_board.sh``).

The rail used to read its credentials and ``DATABASE_URL`` from the Railway CLI
only, so it could not run anywhere the CLI is not installed and logged in. It
now takes ``WEB_AUTH_USER`` / ``WEB_AUTH_PASSWORD`` / ``DATABASE_URL`` from the
environment first, skips the checks whose input is missing, and ends PARTIAL
(exit 2) rather than PASS when it skipped something.

These tests pin the three things that would be expensive to get wrong: an
incomplete run must not read as a pass, a failing ``psql`` must not read as a
pass, and the password must never reach the output.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from base64 import b64encode
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "verify_live_board.sh"

USER = "board"
PASSWORD = "sentinel-password-never-printed"
SHA = "abc1234"
# One row, no quote, with its counter-argument: what the honesty auditor calls a
# pass. The auditor's own rules are pinned in test_audit_script.py.
PAGE = (
    "<html><body><article data-row data-chip='no_quote'>"
    "<p>kotasyon yok</p><p>AMA 1 aile aleyhte: Sektor.</p>"
    "</article></body></html>"
)


class _Board(http.server.BaseHTTPRequestHandler):
    """The live board, reduced to the two routes the rail asks for."""

    def do_GET(self) -> None:  # http.server's spelling, not ours
        if self.path == "/health":
            self._send(200, json.dumps({"ok": True, "sha": SHA}).encode())
            return
        expected = "Basic " + b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="board"')
            self.end_headers()
            return
        self._send(200, PAGE.encode())

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def board_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A PATH whose ``railway`` is absent-by-failure and whose ``uv`` is a no-op.

    The auditor that ``uv run`` would launch has its own tests; what is under
    test here is the rail's control flow, which must not depend on whether the
    machine running the suite happens to have the Railway CLI installed.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    _stub(directory, "railway", "exit 1")
    _stub(directory, "uv", "exit 0")
    return directory


def _run(url: str, stub_bin: Path, env: dict[str, str] | None = None,
         args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for name in ("WEB_AUTH_USER", "WEB_AUTH_PASSWORD", "DATABASE_URL", "RAILWAY_DIR"):
        environment.pop(name, None)
    environment["PATH"] = f"{stub_bin}{os.pathsep}{environment['PATH']}"
    environment["BOARD_URL"] = url
    environment.update(env or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), *(args or [])],
        capture_output=True, text=True, env=environment, timeout=120, check=False,
    )


def test_missing_credentials_skip_and_never_pass(board_url: str, stub_bin: Path) -> None:
    """No credentials anywhere: the checks that need them skip, the run is PARTIAL."""
    result = _run(board_url, stub_bin)
    assert "/health                      200" in result.stdout
    assert "SKIP" in result.stdout
    assert "VERIFY: PARTIAL" in result.stdout
    assert "VERIFY: PASS" not in result.stdout
    assert result.returncode == 2


def test_environment_credentials_open_the_board(board_url: str, stub_bin: Path) -> None:
    """The env pair is enough: the rail authenticates without any Railway CLI."""
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD})
    assert "/ without credentials        401" in result.stdout
    assert "/ with credentials           200" in result.stdout
    assert "live render (s)" in result.stdout
    # Only check 4 is left without an input, so the run is PARTIAL, not FAIL.
    assert "VERIFY: PARTIAL" in result.stdout
    assert result.returncode == 2


def test_the_password_never_reaches_the_output(board_url: str, stub_bin: Path) -> None:
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD})
    assert PASSWORD not in result.stdout
    assert PASSWORD not in result.stderr


def test_a_wrong_expected_sha_fails(board_url: str, stub_bin: Path) -> None:
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD},
                  args=["9999999"])
    assert "deployed sha                 FAIL: expected 9999999" in result.stdout
    assert "VERIFY: FAIL" in result.stdout
    assert result.returncode == 1


def test_a_refused_database_is_a_failure_not_a_pass(board_url: str, stub_bin: Path) -> None:
    """The regression: psql's exit status was swallowed by the sed pipeline."""
    _stub(stub_bin, "psql", 'echo "connection to server failed" >&2; exit 2')
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD,
                   "DATABASE_URL": "postgresql://u:p@example.invalid:5432/db"})
    assert "alfa tables                  FAIL: psql exit 2" in result.stdout
    assert "VERIFY: FAIL" in result.stdout
    assert result.returncode == 1


def test_a_reachable_database_completes_the_run(board_url: str, stub_bin: Path) -> None:
    _stub(stub_bin, "psql", 'echo "alfa_signal | 12"; exit 0')
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD,
                   "DATABASE_URL": "postgresql://u:p@example.invalid:5432/db"})
    assert "alfa table  alfa_signal | 12" in result.stdout
    assert "VERIFY: PASS" in result.stdout
    assert result.returncode == 0


def test_a_connection_string_in_psql_output_is_redacted(board_url: str, stub_bin: Path) -> None:
    _stub(stub_bin, "psql", 'echo "could not connect to postgresql://u:secret@host/db"; exit 0')
    result = _run(board_url, stub_bin,
                  {"WEB_AUTH_USER": USER, "WEB_AUTH_PASSWORD": PASSWORD,
                   "DATABASE_URL": "postgresql://u:secret@host/db"})
    assert "secret@host" not in result.stdout
    assert "<redacted>" in result.stdout
