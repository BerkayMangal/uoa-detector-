"""Private context presentation never fetches or changes live advice on GET."""
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.shared_news.model import FeedState, Item, SharedNewsView, Story

from tests.unit._webapp_auth import set_web_auth


def test_shared_context_routes_are_private_read_only_and_unknown_without_storage(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: datetime(2026, 11, 4, 12, tzinfo=UTC))
    monkeypatch.setenv("SHARED_NEWS_ENABLED", "true")
    writes = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")):
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    for path in ("/haberler", "/api/arastirma/haber-kaynaklari"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "noindex" in response.headers["x-robots-tag"]
    body = client.get("/api/arastirma/haber-kaynaklari", headers=headers).json()
    assert body["role"] == "context_only" and body["supports_negative_evidence"] is False
    assert all(f["status"] == "unknown" for f in body["feeds"])
    assert body["stories"] == [] and writes == []
    page = client.get("/haberler", headers=headers).text
    assert "getiriye ek katkısı henüz doğrulanmadı" in page
    assert "piyasada haber olmadığı anlamına gelmez" in page


def test_valid_context_is_connected_while_curated_empty_never_becomes_no_news(monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    view = SharedNewsView(as_of=now, enabled=True, profile_hash="synthetic", stories=(), feeds=(
        FeedState(provider="mahalle", status="context", reason="curated_context_only", coverage="curated_digest",
                  returned_count=204, limited=True, truncated=False, last_successful_check_at=now),
        FeedState(provider="stajyer", status="unknown", reason="empty_curated_unknown", coverage="curated_note_sources",
                  returned_count=0, last_successful_check_at=now, upstream_status="partial"),
    ))
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=None))
    monkeypatch.setattr(main.shared_news, "build", lambda *args: view)
    client = TestClient(main.app)
    page = client.get("/haberler", headers=set_web_auth(monkeypatch)).text
    assert "Bağlı" in page and "Erişim var · seçilmiş başlık yok" in page
    assert "kapsam kısmi" in page and "piyasada haber olmadığı anlamına gelmez" in page
    assert 'href="/haberler"' in page
    assert 'data-upstream-partial="stajyer"' in page


def test_unverified_publisher_clocks_and_headlines_cannot_be_presented_as_facts(monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    item = Item(provider="stajyer", id="sample", content_version_hash="synthetic", source_id=None,
                source="Catalog", source_title="<script>bad()</script>", title_provenance="catalog_unverified",
                url="https://example.com/article", source_published_at=None,
                source_publication_raw="2030-01-01T00:00:00", publication_precision="unknown",
                publication_claim="upstream_catalog_value", first_seen_at="2020-01-01T00:00:00+00:00",
                exact_tickers=(), derived_tickers=("NVDA",), ticker_mapping_status="unknown",
                model_annotations={}, upstream_job_started_at=None,
                quality_flags=("publication_unknown", "upstream_first_seen_unverified"), first_known_at=now)
    view = SharedNewsView(as_of=now, enabled=True, profile_hash="synthetic", feeds=(),
                          stories=(Story(canonical_url=item.url, versions=(item,)),))
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=None))
    monkeypatch.setattr(main.shared_news, "build", lambda *args: view)
    page = TestClient(main.app).get("/haberler", headers=set_web_auth(monkeypatch)).text
    assert "<script>bad()</script>" not in page and "&lt;script&gt;bad()&lt;/script&gt;" in page
    assert "yayın zamanı bilinmiyor" in page
    assert "Doğrulanmamış tarih iddiası: 2030" in page
    assert "Yayıncıda ilk görülme zamanı bilinmiyor" in page
    assert "doğrulanmamış ilk görülme iddiası: 2020" in page
    assert "Doğrudan şirket haberi sayılmaz" in page
