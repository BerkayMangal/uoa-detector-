"""Physical curated context is bounded, append-only and never negative evidence."""
from __future__ import annotations

import asyncio
import gzip
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, inspect, select, update
from sqlalchemy.engine import Engine
from webapp.board.db import make_engine, session_factory
from webapp.shared_news import collector, storage, transport
from webapp.shared_news.config import Endpoint, settings
from webapp.shared_news.model import canonical, digest, parse
from webapp.shared_news.view import build

T = datetime(2026, 11, 4, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("SHARED_NEWS_ENABLED", "true")
    result = make_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    storage.ensure(result)
    return result


def endpoint(provider: str = "mahalle") -> Endpoint:
    return next(p for p in settings().providers if p.provider == provider)


def payload(provider: str = "mahalle", *, items: int = 1, **changes: object) -> bytes:
    entries: list[dict[str, object]] = []
    for index in range(items):
        obj: dict[str, object] = dict(id=f"story-{index}", source_id="original", source="Publisher",
            source_title="Original publisher headline", url=f"https://publisher.test/article/{index}?utm_source=feed",
            source_published_at=(T-timedelta(hours=1)).isoformat(),
            source_publication_raw=(T-timedelta(hours=1)).isoformat(), publication_precision="timestamp",
            publication_claim="upstream_catalog_value", first_seen_at=None, exact_tickers=["NVDA"],
            derived_tickers=["AMD"], ticker_mapping_status="upstream_explicit", model_annotations={"headline": "Model opinion"})
        if provider == "mahalle":
            obj["supports_negative_evidence"] = False
        else:
            obj["title_provenance"] = "upstream_publisher_metadata"
        obj.update(changes)
        basis = {k: v for k, v in obj.items() if provider == "mahalle" or k != "id"}
        obj["content_version_hash"] = digest(canonical(basis))
        entries.append(obj)
    return canonical(dict(schema="context_news_v1", provider=provider, coverage=endpoint(provider).coverage,
                          supports_negative_evidence=False, returned_count=len(entries), items=entries))


def append(engine: Engine, raw: bytes | None = None, *, provider: str = "mahalle", at: datetime = T,
           available: datetime | None = None, etag: str | None = '"etag"') -> str:
    raw = payload(provider) if raw is None else raw
    return storage.append(engine, endpoint(provider).provider, at-timedelta(seconds=1), at, 200, "ok", raw,
                          etag=etag, parsed=parse(raw, endpoint(provider), at), clock=lambda: available or at+timedelta(seconds=1))


@pytest.mark.parametrize("provider", ["mahalle", "stajyer"])
def test_provider_hash_and_original_provenance(provider: str) -> None:
    result = parse(payload(provider), endpoint(provider), T)
    item = result.items[0]
    assert item.source_title == "Original publisher headline"
    assert item.model_annotations["headline"] == "Model opinion"
    assert item.exact_tickers == ("NVDA",) and item.derived_tickers == ("AMD",)
    assert item.first_seen_at is None and item.first_known_at is None
    changed = json.loads(payload(provider))
    changed["items"][0]["source_title"] = "Changed without new version"
    with pytest.raises(ValueError, match="content_version_hash"):
        parse(canonical(changed), endpoint(provider), T)


@pytest.mark.parametrize("url", ["http://mahalle-production.up.railway.app/news-feed.json", "https://evil.test/news-feed.json",
    "https://mahalle-production.up.railway.app/run", "https://mahalle-production.up.railway.app/news-feed.json?x=1",
    "https://user@mahalle-production.up.railway.app/news-feed.json", "https://mahalle-production.up.railway.app:443/news-feed.json"])
def test_exact_origin_and_path_rejects_ssrf(url: str) -> None:
    with pytest.raises(ValidationError):
        Endpoint(provider="mahalle", url=url, token_env="MAHALLE_NEWS_FEED_TOKEN", coverage="curated_digest")


@pytest.mark.parametrize("change", [{"provider": "stajyer"}, {"schema": "v2"}, {"supports_negative_evidence": True},
    {"supports_negative_evidence": 0}, {"returned_count": True}, {"returned_count": 2}, {"coverage": "full_market"}])
def test_invalid_envelopes_fail_closed(change: dict[str, object]) -> None:
    value = json.loads(payload()) | change
    with pytest.raises(ValueError):
        parse(canonical(value), endpoint(), T)


@pytest.mark.parametrize("value", [b'{"unused":NaN}', b'{"unused":Infinity}', b'{"unused":1e999}'])
def test_nonfinite_json_rejected(value: bytes) -> None:
    with pytest.raises(ValueError):
        parse(value, endpoint(), T)


@pytest.mark.parametrize("publication", ["2026-11-04T14:00:00", "2026-11-05T14:00:00Z", "bad"])
def test_naive_future_malformed_publication_unknown_not_backdated(publication: str) -> None:
    item = parse(payload(source_published_at=publication, source_publication_raw=publication), endpoint(), T).items[0]
    assert item.source_published_at is None and item.publication_precision == "unknown"
    assert item.source_publication_raw == publication and "publication_unknown" in item.quality_flags


def test_date_only_and_unknown_upstream_first_seen_preserved() -> None:
    item = parse(payload(source_published_at="2026-11-03", publication_precision="date"), endpoint(), T).items[0]
    assert item.source_published_at == "2026-11-03" and item.publication_precision == "date"
    assert item.first_seen_at is None
    future = parse(payload(first_seen_at="2026-11-05T00:00:00Z"), endpoint(), T).items[0]
    assert future.first_seen_at is None and "first_seen_unknown" in future.quality_flags


def test_unverified_stajyer_title_stays_model_annotation() -> None:
    item = parse(payload("stajyer", title_provenance="unverified_catalog_value"), endpoint("stajyer"), T).items[0]
    assert item.source_title is None
    assert item.model_annotations["unverified_catalog_title"] == "Original publisher headline"
    assert item.title_provenance == "unverified_catalog_value"


def test_item_limit_is_explicit_curated_truncation() -> None:
    result = parse(payload(items=101), endpoint(), T)
    assert len(result.items) == 100 and result.returned_count == 101 and result.limited and result.truncated
    with pytest.raises(ValueError, match="overlap"):
        parse(payload(derived_tickers=["NVDA"]), endpoint(), T)


def test_identical_source_copies_display_once_with_original_count() -> None:
    value = json.loads(payload())
    value["items"] *= 2
    value["returned_count"] = 2
    result = parse(canonical(value), endpoint(), T)
    assert result.returned_count == 2 and len(result.items) == 1


def test_post_commit_receipt_and_read_only_asof(engine: Engine) -> None:
    commits: list[bool] = []
    statements: list[str] = []
    event.listen(engine, "commit", lambda _: commits.append(True))
    def clock() -> datetime:
        assert len(commits) == 1
        with session_factory(engine)() as session:
            assert session.scalar(select(func.count()).select_from(storage.Packet)) == 1
        return T+timedelta(seconds=2)
    raw = payload()
    storage.append(engine, "mahalle", T-timedelta(seconds=1), T, 200, "ok", raw, parsed=parse(raw, endpoint(), T), clock=clock)
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, parameters, context, executemany: statements.append(sql))
    assert build(engine, T+timedelta(seconds=1)).feeds[0].reason == "receipt_pending_or_invalid"
    item = build(engine, T+timedelta(seconds=2)).feeds[0].items[0]
    assert item.first_known_at == T+timedelta(seconds=2)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for sql in statements)


def test_repeated_versions_first_known_and_new_version(engine: Engine) -> None:
    first = append(engine)
    assert append(engine) == first
    append(engine, at=T+timedelta(seconds=10))
    repeated = build(engine, T+timedelta(seconds=12)).feeds[0]
    assert repeated.items[0].first_known_at == T+timedelta(seconds=1)
    assert repeated.last_new_content_at == T+timedelta(seconds=1)
    assert repeated.last_successful_check_at == T+timedelta(seconds=11)
    append(engine, payload(source_title="New original headline"), at=T+timedelta(seconds=20))
    changed = build(engine, T+timedelta(seconds=22)).feeds[0]
    assert changed.items[0].first_known_at == T+timedelta(seconds=21)
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(storage.Packet)) == 3
        assert session.scalar(select(func.count()).select_from(storage.Receipt)) == 3
        assert session.scalar(select(func.count()).select_from(storage.Observation)) == 3


def test_future_packet_does_not_change_past_view(engine: Engine) -> None:
    append(engine)
    past = build(engine, T+timedelta(seconds=2))
    append(engine, at=T+timedelta(days=1))
    assert build(engine, T+timedelta(seconds=2)) == past


def test_failure_empty_stale_and_missing_receipt_never_no_news(engine: Engine) -> None:
    append(engine)
    storage.append(engine, "mahalle", T+timedelta(seconds=3), T+timedelta(seconds=4), 503, "http_error", clock=lambda: T+timedelta(seconds=5))
    assert build(engine, T+timedelta(seconds=6)).feeds[0].reason == "http_error"
    append(engine, payload(items=0), at=T+timedelta(seconds=10))
    state = build(engine, T+timedelta(seconds=12)).feeds[0]
    assert state.status == "unknown" and state.reason == "empty_curated_unknown" and state.supports_negative_evidence is False
    append(engine, at=T+timedelta(seconds=20))
    assert build(engine, T+timedelta(hours=2)).feeds[0].status == "stale"
    with pytest.raises(ValueError, match="availability"):
        append(engine, at=T+timedelta(hours=3), available=T)
    assert build(engine, T+timedelta(hours=3, seconds=1)).feeds[0].reason == "receipt_pending_or_invalid"


@pytest.mark.parametrize("damage", ["body", "receipt", "metadata", "item"])
def test_tampering_fails_closed(engine: Engine, damage: str) -> None:
    key = append(engine)
    with engine.begin() as connection:
        if damage == "body":
            connection.execute(update(storage.Packet).where(storage.Packet.key == key).values(compressed_body=gzip.compress(b"{}")))
        elif damage == "receipt":
            connection.execute(update(storage.Receipt).where(storage.Receipt.key == key).values(packet_hash="wrong"))
        elif damage == "metadata":
            connection.execute(update(storage.Packet).where(storage.Packet.key == key).values(category="not_modified"))
        else:
            connection.execute(update(storage.Observation).values(item_json="{}"))
    assert build(engine, T+timedelta(seconds=2)).feeds[0].status == "unknown"


def test_cross_provider_duplicates_keep_provenance_without_votes(engine: Engine) -> None:
    append(engine)
    append(engine, provider="stajyer")
    view = build(engine, T+timedelta(seconds=2))
    assert len(view.stories) == 1 and len(view.stories[0].versions) == 2
    assert {i.provider for i in view.stories[0].versions} == {"mahalle", "stajyer"}
    assert view.stories[0].independent_votes is False


def test_partial_producer_status_survives_read_only_json_view(engine: Engine) -> None:
    value = json.loads(payload("stajyer"))
    value["status"] = "partial"
    append(engine, canonical(value), provider="stajyer")
    view = build(engine, T+timedelta(seconds=2))
    assert view.feeds[1].status == "context"
    assert view.feeds[1].upstream_status == "partial"
    assert view.model_dump(mode="json")["feeds"][1]["upstream_status"] == "partial"


async def test_304_preserves_original_availability(engine: Engine) -> None:
    append(engine)
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"etag"'
        assert request.headers["authorization"] == "Bearer dedicated-mahalle"
        return httpx.Response(304, headers={"etag": '"etag"'})
    at = T+timedelta(seconds=10)
    assert await collector.collect(engine, endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: at)
    state = build(engine, at).feeds[0]
    assert state.status == "context" and state.items[0].first_known_at == T+timedelta(seconds=1)
    assert state.last_successful_check_at == at and state.last_new_content_at == T+timedelta(seconds=1)


async def test_orphan_304_and_unauthenticated_304(engine: Engine) -> None:
    calls: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(304)
    await collector.collect(engine, endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: T)
    assert build(engine, T).feeds[0].reason == "orphan_not_modified"
    response = await transport.fetch(endpoint(), "", transport=httpx.MockTransport(handler), clock=lambda: T)
    assert response.category == "credential_unavailable" and len(calls) == 1


@pytest.mark.parametrize("first,second", [('W/"opaque"', '"opaque"'), ('"opaque"', 'W/"opaque"')])
async def test_304_weak_and_strong_transport_tags_are_equivalent(engine: Engine, first: str, second: str) -> None:
    append(engine, etag=first)
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == first
        return httpx.Response(304, headers={"etag": second})
    at = T+timedelta(seconds=10)
    await collector.collect(engine, endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: at)
    state = build(engine, at).feeds[0]
    assert state.status == "context" and state.last_new_content_at == T+timedelta(seconds=1)


async def test_304_different_opaque_tag_fails_closed(engine: Engine) -> None:
    append(engine, etag='W/"original"')
    at = T+timedelta(seconds=10)
    await collector.collect(engine, endpoint(), "dedicated-mahalle",
        transport=httpx.MockTransport(lambda _: httpx.Response(304, headers={"etag": '"changed"'})), clock=lambda: at)
    assert build(engine, at).feeds[0].reason == "orphan_not_modified"


def test_last_success_survives_more_than_read_limit_failures(engine: Engine) -> None:
    append(engine)
    for index in range(settings().read_packet_limit+1):
        at = T+timedelta(seconds=index+3)
        storage.append(engine, "mahalle", at, at, 500, "http_error", clock=lambda: at)
    state = build(engine, T+timedelta(minutes=1)).feeds[0]
    assert state.last_successful_check_at == T+timedelta(seconds=1)
    assert state.status == "unknown" and state.reason == "http_error"


@pytest.mark.parametrize("code", [301, 302, 307, 308, 401, 403, 500])
async def test_no_redirect_or_retry_or_host_token_leak(code: int) -> None:
    calls: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == endpoint().url
        return httpx.Response(code, headers={"location": "https://evil.test/run"})
    result = await transport.fetch(endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: T)
    assert result.category == ("redirect_rejected" if code < 400 else "http_error")
    assert len(calls) == 1


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"x" * settings().max_body_bytes
        yield b"x"


@pytest.mark.parametrize("declared", [True, False])
async def test_body_limit_before_and_during_stream(declared: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": str(settings().max_body_bytes+1)} if declared else {}
        return httpx.Response(200, headers=headers, stream=Chunks())
    result = await transport.fetch(endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: T)
    assert result.category == "body_limit" and result.raw == b""


async def test_compressed_content_rejected_before_decompression() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=gzip.compress(b"x" * 2000000))
    result = await transport.fetch(endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(handler), clock=lambda: T)
    assert result.category == "content_encoding_rejected"


async def test_request_timeout_and_total_slow_stream_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=payload())
    monkeypatch.setattr(transport, "settings", lambda: settings().model_copy(update={"request_timeout_seconds": 0.01}))
    result = await transport.fetch(endpoint(), "dedicated-mahalle", transport=httpx.MockTransport(slow), clock=lambda: T)
    assert result.category == "request_timeout"


async def test_storage_timeout_does_not_start_thread_backlog(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[bool] = []
    original = storage.append
    def slow(*args: object, **kwargs: object) -> str:
        calls.append(True)
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(storage, "append", slow)
    monkeypatch.setattr(collector, "settings", lambda: settings().model_copy(update={"recording_timeout_seconds": 0.01}))
    raw = payload()
    response = transport.Response(T-timedelta(seconds=1), T, 200, "ok", raw, parsed=parse(raw, endpoint(), T))
    try:
        assert not await collector._write(engine, endpoint(), response, None, lambda: T+timedelta(seconds=1))
        assert entered.is_set()
        assert not await collector._write(engine, endpoint(), response, None, lambda: T+timedelta(seconds=1))
        assert len(calls) == 1
    finally:
        release.set()
        for _ in range(100):
            if not collector._WRITER.locked():
                break
            await asyncio.sleep(0.01)
    assert build(engine, T+timedelta(seconds=2)).feeds[0].reason == "recording_unavailable"


async def test_disabled_loop_no_engine_or_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SHARED_NEWS_ENABLED", raising=False)
    await collector.loop("not-a-database")
    result = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    view = build(result, T)
    assert not view.enabled and all(f.status == "disabled" for f in view.feeds)
    assert inspect(result).get_table_names() == []
