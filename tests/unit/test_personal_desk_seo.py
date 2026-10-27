"""Public presentation never opens private data or weakens indexing/auth guards."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from webapp import main, seo
from webapp.board.db import make_engine
from webapp.live_alpha import board, store
from webapp.live_alpha import page as live_page

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_live_board_baskets import _lag

PUBLIC = {"/health", "/hakkinda", "/robots.txt", "/sitemap.xml", "/assets/uoa.css", "/favicon.svg"}


def test_public_surface_is_exact_and_get_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert seo.PUBLIC_GET_PATHS == PUBLIC
    set_web_auth(monkeypatch)
    client = TestClient(main.app)
    for path in PUBLIC:
        assert client.get(path).status_code == 200, path
        for method in ("POST", "HEAD", "OPTIONS"):
            assert client.request(method, path).status_code == 401, (method, path)
    for path in ("/", "/sonuclar", "/journal", "/openapi.json", "/docs", "/assets/private.db",
                 "/assets/%2e%2e/main.py", "/hakkinda/private", "/hakkinda/", "/.env"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 401, path
        assert response.headers["cache-control"] == "private, no-store"
        assert "noindex" in response.headers["x-robots-tag"]


def test_public_introduction_has_seo_but_never_reads_private_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Any:
        pytest.fail("Public introduction must not read the trading database")
    monkeypatch.setattr(main, "_board_reader", forbidden)
    monkeypatch.setattr(main, "_repo", forbidden)
    monkeypatch.delenv("WEB_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    response = TestClient(main.app).get("/hakkinda", headers={"Host": "attacker.example"})
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "index, follow"
    assert 'lang="tr"' in response.text
    assert '<meta name="robots" content="index, follow">' in response.text
    assert f'href="{seo.ORIGIN}/hakkinda"' in response.text
    assert "attacker.example" not in response.text
    assert "doğrulanmış bir işlem avantajı göstermemiştir" in response.text
    assert "data-card=" not in response.text and "data-paper=" not in response.text
    structured = re.search(r'<script type="application/ld\+json">(.*?)</script>', response.text)
    assert structured is not None
    data = json.loads(structured[1])
    assert data["@type"] == "SoftwareApplication" and "aggregateRating" not in data
    assert "cdn.tailwindcss.com" not in response.text
    assert '/assets/uoa.css?v=' in response.text


def test_robots_and_sitemap_only_advertise_public_introduction() -> None:
    client = TestClient(main.app)
    robots = client.get("/robots.txt")
    assert "Disallow: /" in robots.text and "Allow: /hakkinda$" in robots.text
    sitemap = client.get("/sitemap.xml")
    root = ElementTree.fromstring(sitemap.text)
    urls = [node.text for node in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert urls == [seo.ORIGIN + "/hakkinda"]
    assert client.get("/health").headers["cache-control"] == "no-store"


def test_private_authenticated_html_is_not_indexable(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = set_web_auth(monkeypatch)
    response = TestClient(main.app).get("/no-such-page", headers=headers)
    assert response.status_code == 404
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.headers["cache-control"] == "private, no-store"
    engine = make_engine("sqlite:///:memory:")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    try:
        page = TestClient(main.app).get("/sonuclar", headers=headers)
        assert page.status_code == 200
        assert '<title>Sonuçlar · UOA</title>' in page.text
        assert '<meta name="robots" content="noindex, nofollow, nosnippet">' in page.text
        assert 'rel="canonical"' not in page.text
        assert "noindex" in page.headers["x-robots-tag"]
        assert page.headers["cache-control"] == "private, no-store"
    finally:
        engine.dispose()


def test_public_render_failure_is_never_indexable(monkeypatch: pytest.MonkeyPatch) -> None:
    render = main.templates.TemplateResponse
    def broken(*args: Any, **kwargs: Any) -> Any:
        if len(args) > 1 and args[1] == "about.html":
            raise RuntimeError("render failed")
        return render(*args, **kwargs)
    monkeypatch.setattr(main.templates, "TemplateResponse", broken)
    response = TestClient(main.app, raise_server_exceptions=False).get("/hakkinda")
    assert response.status_code == 500
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(("lagging", "leading"), [(True, False), (False, True), (False, False)])
def test_focus_filter_uses_measured_lag_flag_not_shared_amber_colour(lagging: bool, leading: bool) -> None:
    rows = board.build_rows(["AAOI"], {}, {}, {}, {}, {}, {}, _lag().end, board.load_board_settings(),
                            basket={"AAOI": _lag(lagging=lagging, leading=leading)})
    assert rows[0]["basket_lagging"] is lagging


def test_paused_refresh_entry_lifetime_is_capped_by_scan_age_and_session_close() -> None:
    engine = make_engine("sqlite:///:memory:")
    store.ensure_live_tables(engine)
    now = datetime(2026, 9, 24, 15, tzinfo=UTC)
    store.write_scan(engine, store.ScanRow("scan", now, now, "LIVE", "ok", "run", {}), "v1")
    try:
        assert live_page.build_view(engine, now + timedelta(seconds=60)).entry_ttl_seconds == 840
        assert live_page.build_view(engine, now + timedelta(minutes=16)).entry_ttl_seconds == 0
        close = now.replace(hour=19, minute=59, second=30)
        store.write_scan(engine, store.ScanRow("close", close, close, "LIVE", "ok", "run", {}), "v1")
        assert live_page.build_view(engine, close).entry_ttl_seconds == 30
        assert live_page.build_view(engine, close + timedelta(seconds=31)).entry_ttl_seconds == 0
    finally:
        engine.dispose()
