"""An unavailable storage operation cannot be presented as a confirmed transaction."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.requests import Request
from webapp import main

from tests.unit._webapp_auth import set_web_auth

SECRET = "password=PRIVATE postgres://PRIVATE Traceback private-provider-data"


@pytest.fixture
def outage(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fail(*_: Any) -> Any:
        raise OperationalError(SECRET, {"input": SECRET}, Exception(SECRET))

    monkeypatch.setattr(main, "_board_reader", fail)
    return TestClient(main.app, headers=set_web_auth(monkeypatch), raise_server_exceptions=False)


def test_private_get_shows_clear_503_without_driver_payload(outage: TestClient) -> None:
    response = outage.get("/stratejiler")
    assert response.status_code == 503
    assert "Veriye şu an erişilemiyor" in response.text
    assert "güncel kabul etmeyin" in response.text
    assert "PRIVATE" not in response.text and "Nothing is lost" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    assert 'http-equiv="refresh"' not in response.text


def test_failed_mutation_has_unknown_outcome_and_is_not_automatically_replayed(
    outage: TestClient,
) -> None:
    response = outage.post(
        "/canli/izle",
        data={"opportunity_id": "PRIVATE"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert "Kayıt durumu doğrulanamıyor" in response.text
    assert "kaydedilip kaydedilmediğini doğrulayamıyoruz" in response.text
    assert "otomatik olarak tekrar gönderilmedi" in response.text
    assert "PRIVATE" not in response.text and "Nothing is lost" not in response.text
    assert "location" not in response.headers and "retry-after" not in response.headers


def test_api_read_returns_fixed_json_not_html_or_raw_error(outage: TestClient) -> None:
    response = outage.get("/api/canli/uyarilar")
    assert response.status_code == 503
    assert response.json() == {"error": "database_unavailable", "outcome": "unavailable"}
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_api_mutation_reports_unknown_outcome_without_retry_suggestion() -> None:
    import json

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/write",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )
    response = await main._database_unavailable(
        request, OperationalError(SECRET, {}, Exception(SECRET))
    )
    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "database_unavailable", "outcome": "unknown"}
    assert "retry-after" not in response.headers


def test_auth_and_public_pages_do_not_become_database_error_views(outage: TestClient) -> None:
    response = outage.get("/stratejiler", headers={"Authorization": "Basic invalid"})
    assert response.status_code == 401
    assert outage.get("/hakkinda").status_code == 200
    assert outage.get("/hakkinda").headers["x-robots-tag"] == "index, follow"
    assert outage.get("/health").status_code == 200


def test_non_operational_errors_keep_existing_error_semantics(
    outage: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> Any:
        raise IntegrityError(SECRET, {}, Exception(SECRET))

    monkeypatch.setattr(main, "_board_reader", fail)
    response = outage.get("/stratejiler")
    assert response.status_code == 500 and "Temporarily unavailable" in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_render_failure_still_does_not_falsely_confirm_transaction_or_leak_error(
    outage: TestClient, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    def fail(*_: Any, **__: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(main.templates, "TemplateResponse", fail)
    response = (
        outage.get("/stratejiler")
        if method == "GET"
        else outage.post(
            "/canli/izle", data={"opportunity_id": "id"}, headers={"origin": "http://testserver"}
        )
    )
    assert response.status_code == 503 and "PRIVATE" not in response.text
    if method == "POST":
        assert "Kaydın tamamlandığını doğrulayamıyoruz" in response.text
