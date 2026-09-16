"""Phase 5.0.9: HTTP Basic auth, fail closed (contract phase-5.0-merge §3.10).

Every entry in ``app.routes`` except ``/health`` is enumerated, so a route
added later is covered without editing this file. Path parameters are filled
with a dummy value and each route is exercised with each of its methods.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from webapp import main as webapp_main

from tests.unit._webapp_auth import TEST_PASSWORD, TEST_USER, basic_auth_header

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CHALLENGE = 'Basic realm="uoa"'
_CORRECT = basic_auth_header(TEST_USER, TEST_PASSWORD)


def _route_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for route in webapp_main.app.routes:
        raw_path = getattr(route, "path", None)
        if not isinstance(raw_path, str) or raw_path == "/health":
            continue
        path = re.sub(r"\{[^}]*\}", "dummy", raw_path) or "/"
        methods = getattr(route, "methods", None) or {"GET"}
        cases.extend((method, path) for method in sorted(methods))
    return cases


_CASES = _route_cases()
_CASE_IDS = [f"{method} {path}" for method, path in _CASES]

_WRONG = {
    "wrong password": basic_auth_header(TEST_USER, "not-the-pass"),
    "wrong user": basic_auth_header("not-the-user", TEST_PASSWORD),
    "swapped": basic_auth_header(TEST_PASSWORD, TEST_USER),
}

_UNCONFIGURED: dict[str, dict[str, str]] = {
    "both unset": {},
    "user unset": {"WEB_AUTH_PASSWORD": TEST_PASSWORD},
    "password unset": {"WEB_AUTH_USER": TEST_USER},
    "user empty": {"WEB_AUTH_USER": "", "WEB_AUTH_PASSWORD": TEST_PASSWORD},
    "password empty": {"WEB_AUTH_USER": TEST_USER, "WEB_AUTH_PASSWORD": ""},
}


def _apply_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.delenv("WEB_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch, {"WEB_AUTH_USER": TEST_USER, "WEB_AUTH_PASSWORD": TEST_PASSWORD})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    webapp_main._REPO = webapp_main._JOURNAL = webapp_main._GAMMA = None
    yield TestClient(webapp_main.app, raise_server_exceptions=False)
    webapp_main._REPO = webapp_main._JOURNAL = webapp_main._GAMMA = None


def test_route_enumeration_covers_the_app() -> None:
    assert ("GET", "/") in _CASES
    assert ("GET", "/gamma") in _CASES
    assert ("GET", "/journal") in _CASES
    assert ("POST", "/journal") in _CASES
    assert ("GET", "/journal/new") in _CASES
    assert ("POST", "/journal/dummy/close") in _CASES
    assert ("GET", "/openapi.json") in _CASES
    assert all(path != "/health" for _method, path in _CASES)


@pytest.mark.parametrize(("method", "path"), _CASES, ids=_CASE_IDS)
def test_missing_credentials_is_401_with_challenge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str,
) -> None:
    _configure(monkeypatch)
    r = client.request(method, path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == _CHALLENGE


@pytest.mark.parametrize("wrong", list(_WRONG), ids=list(_WRONG))
@pytest.mark.parametrize(("method", "path"), _CASES, ids=_CASE_IDS)
def test_wrong_credentials_is_401_with_challenge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, wrong: str,
) -> None:
    _configure(monkeypatch)
    r = client.request(method, path, headers=_WRONG[wrong])
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == _CHALLENGE


@pytest.mark.parametrize(
    "authorization",
    [
        f"Bearer {TEST_PASSWORD}",
        "Basic",
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"no-colon-here").decode("ascii"),
    ],
    ids=["bearer scheme", "empty token", "bad base64", "no colon"],
)
def test_malformed_authorization_is_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, authorization: str,
) -> None:
    _configure(monkeypatch)
    r = client.get("/", headers={"Authorization": authorization})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == _CHALLENGE


@pytest.mark.parametrize(("method", "path"), _CASES, ids=_CASE_IDS)
def test_correct_credentials_pass_the_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str,
) -> None:
    _configure(monkeypatch)
    r = client.request(method, path, headers=_CORRECT)
    assert r.status_code not in (401, 503)


@pytest.mark.parametrize("send_credentials", [False, True], ids=["no credentials", "credentials"])
@pytest.mark.parametrize("env", list(_UNCONFIGURED.values()), ids=list(_UNCONFIGURED))
@pytest.mark.parametrize(("method", "path"), _CASES, ids=_CASE_IDS)
def test_unconfigured_gate_is_503_with_no_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str,
    env: dict[str, str], send_credentials: bool,
) -> None:
    _apply_env(monkeypatch, env)
    r = client.request(method, path, headers=_CORRECT if send_credentials else None)
    assert r.status_code == 503
    assert "www-authenticate" not in r.headers
    if method != "HEAD":
        assert r.text == "auth not configured"


_HEALTH_ENVS: dict[str, dict[str, str]] = {
    "configured": {"WEB_AUTH_USER": TEST_USER, "WEB_AUTH_PASSWORD": TEST_PASSWORD},
    **_UNCONFIGURED,
}


@pytest.mark.parametrize(
    "headers", [None, _WRONG["wrong password"], _CORRECT],
    ids=["no credentials", "wrong credentials", "correct credentials"],
)
@pytest.mark.parametrize("env", list(_HEALTH_ENVS.values()), ids=list(_HEALTH_ENVS))
def test_health_is_open_in_every_case(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str], headers: dict[str, str] | None,
) -> None:
    _apply_env(monkeypatch, env)
    r = client.get("/health", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_unknown_path_is_gated_before_routing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    assert client.get("/no-such-page").status_code == 401
    assert client.get("/no-such-page", headers=_CORRECT).status_code == 404


def test_credentials_never_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _configure(monkeypatch)
    client.get("/", headers=_WRONG["wrong password"])
    client.get("/", headers=_CORRECT)
    _apply_env(monkeypatch, {"WEB_AUTH_USER": TEST_USER})
    client.get("/", headers=_CORRECT)
    for secret in (TEST_USER, TEST_PASSWORD, "not-the-pass"):
        assert secret not in caplog.text


def test_lifespan_passes_through_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # lifespan starts no live task
    _configure(monkeypatch)
    with TestClient(webapp_main.app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/journal").status_code == 401
