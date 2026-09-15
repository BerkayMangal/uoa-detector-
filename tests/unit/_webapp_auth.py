"""Shared HTTP Basic credentials for webapp TestClient tests (Phase 5.0.9).

The webapp gates every route except GET /health behind HTTP Basic auth
(contract phase-5.0-merge §3.10). Route tests configure dummy credentials in
the environment and send them on every request; their assertions are unchanged.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    import pytest
    from starlette.types import ASGIApp

TEST_USER = "route-test-user"
TEST_PASSWORD = "route-test-pass"


def basic_auth_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def set_web_auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Configure the gate for this test; return a matching Authorization header."""
    monkeypatch.setenv("WEB_AUTH_USER", TEST_USER)
    monkeypatch.setenv("WEB_AUTH_PASSWORD", TEST_PASSWORD)
    return basic_auth_header(TEST_USER, TEST_PASSWORD)


def authed_client(app: ASGIApp, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient that sends valid credentials on every request."""
    return TestClient(app, headers=set_web_auth(monkeypatch))
