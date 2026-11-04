"""Physical news recording preserves live responses and conservative as-of evidence."""
from __future__ import annotations

import asyncio
import gzip
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.news_research import storage, transport

T = datetime(2026, 11, 4, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    result = make_engine(f"sqlite:///{tmp_path / 'news.db'}")
    storage.ensure(result)
    return result


def article(**changes: object) -> dict[str, object]:
    return dict(headline="NVDA wins contract", source="Newswire", tickers=["NVDA"], created_at=(T - timedelta(hours=1)).isoformat()) | changes


def append(engine: Engine, payload: object, *, at: datetime = T, available: datetime | None = None, succeeded: bool = True) -> str:
    return storage.append(engine, "NVDA", payload, at-timedelta(seconds=1), at,
                          clock=lambda: available or at+timedelta(seconds=1), succeeded=succeeded)


def test_post_commit_clock_and_exact_available_boundary(engine: Engine) -> None:
    commits = []
    event.listen(engine, "commit", lambda _: commits.append(True))

    def clock() -> datetime:
        assert len(commits) == 1
        with Session(engine) as s:
            assert s.scalar(select(func.count()).select_from(storage.Packet)) == 1
        return T + timedelta(seconds=2)

    storage.append(engine, "NVDA", {"data": [article()]}, T-timedelta(seconds=1), T, clock=clock)
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=1)).flag is None
    result = storage.evidence(engine, "NVDA", T+timedelta(seconds=2))
    assert result.flag is True and result.available_at == T+timedelta(seconds=2)


def test_idempotent_packet_does_not_move_first_availability(engine: Engine) -> None:
    first = append(engine, {"data": [article()]})
    second = append(engine, {"data": [article()]}, available=T+timedelta(seconds=30))
    assert first == second
    result = storage.evidence(engine, "NVDA", T+timedelta(seconds=2))
    assert result.available_at == T+timedelta(seconds=1)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(storage.Receipt)) == 1


def test_absent_storage_and_missing_receipt_are_unknown(tmp_path: Path, engine: Engine) -> None:
    empty = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert storage.evidence(empty, "NVDA", T).flag is None
    with pytest.raises(ValueError, match="availability"):
        append(engine, {"data": []}, available=T-timedelta(seconds=1))
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2)).reason == "physical_receipt_missing"


@pytest.mark.parametrize("body,reason", [
    ({}, "news_payload_shape_invalid"),
    ({"data": None}, "news_payload_shape_invalid"),
    ({"data": [None]}, "news_item_shape_invalid"),
    ({"data": [article(headline="")]}, "news_headline_invalid"),
    ({"data": [article(tickers=None)]}, "news_ticker_mapping_unknown"),
    ({"data": [article(created_at="2026-11-04T14:00:00")]}, "news_publication_unknown"),
    ({"data": [article(created_at=(T+timedelta(seconds=1)).isoformat())]}, "news_publication_after_retrieval"),
    ({"data": [article(updated_at=T.isoformat())]}, "news_revision_unknown"),
    ({"data": [article(id="one"), article(id="one", headline="NVDA cuts forecast")]}, "news_conflicting_versions"),
])
def test_bad_or_ambiguous_payload_does_not_become_no_news(engine: Engine, body: object, reason: str) -> None:
    append(engine, body)
    result = storage.evidence(engine, "NVDA", T+timedelta(seconds=2))
    assert result.flag is None and result.reason == reason


def test_empty_foreign_stale_and_duplicate_headlines(engine: Engine) -> None:
    append(engine, {"data": [article(tickers=["NVDAX"])]})
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2)).flag is False
    append(engine, {"data": [article(created_at=(T-timedelta(hours=40)).isoformat())]}, at=T+timedelta(seconds=3))
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=5)).flag is False
    append(engine, {"data": [article(), article(headline="NVDA wins CONTRACT!"), article(headline="NVDA cuts forecast", sentiment="negative")]}, at=T+timedelta(seconds=6))
    result = storage.evidence(engine, "NVDA", T+timedelta(seconds=8))
    assert result.flag is True and len(result.headlines) == 2
    assert storage.evidence(engine, "NVDA", T+timedelta(minutes=16)).flag is None


def test_later_failure_or_malformed_check_blocks_old_success(engine: Engine) -> None:
    append(engine, {"data": [article()]})
    append(engine, {"failure": "TimeoutError"}, at=T+timedelta(seconds=3), succeeded=False)
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=5)).reason == "news_retrieval_failed"
    append(engine, {}, at=T+timedelta(seconds=6))
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=8)).flag is None
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2)).flag is True


@pytest.mark.parametrize("damage", ["payload", "receipt", "metadata"])
def test_hash_receipts_cover_payload_and_metadata(engine: Engine, damage: str) -> None:
    key = append(engine, {"data": [article()]})
    with engine.begin() as c:
        if damage == "payload":
            c.execute(update(storage.Packet).where(storage.Packet.key==key).values(compressed_payload=gzip.compress(json.dumps({"data": []}).encode())))
        elif damage == "metadata":
            c.execute(update(storage.Packet).where(storage.Packet.key==key).values(requested_at=T-timedelta(seconds=3)))
        else:
            c.execute(update(storage.Receipt).where(storage.Receipt.key==key).values(packet_hash="wrong"))
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2)).flag is None


class Fake:
    last_daily_request_count = 17

    def __init__(self, response: dict[str, Any], error: Exception | None = None) -> None:
        self.response, self.error = response, error
        self.calls: list[tuple[str, dict[str, Any] | None, str]] = []

    async def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
        self.calls.append((path, params, method))
        if self.error:
            raise self.error
        return self.response


def test_wrapper_makes_one_call_preserves_identity_and_quota_metadata(engine: Engine) -> None:
    payload = {"data": [article()]}
    native = Fake(payload)
    clock = iter((T-timedelta(seconds=1), T, T+timedelta(seconds=1)))
    wrapped = transport.RecordingClient(native, engine, clock=lambda: next(clock), application_checked_at=T-timedelta(seconds=3))
    result = asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA", "limit": 20}))
    assert result is payload and len(native.calls) == 1
    assert wrapped.last_daily_request_count == 17
    assert not hasattr(wrapped, "last_daily_request_at")
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2), check_at=T-timedelta(seconds=3)).flag is True


def test_failed_upstream_is_original_exception_and_failure_tombstone(engine: Engine) -> None:
    append(engine, {"data": [article()]})
    failure = RuntimeError("PRIVATE SECRET SHOULD NOT BE LOGGED")
    native = Fake({}, failure)
    clock = iter((T+timedelta(seconds=3), T+timedelta(seconds=4), T+timedelta(seconds=5)))
    wrapped = transport.RecordingClient(native, engine, clock=lambda: next(clock))
    with pytest.raises(RuntimeError) as got:
        asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"}))
    assert got.value is failure and len(native.calls) == 1
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=6)).flag is None


def test_non_news_requests_are_transparent_without_storage(tmp_path: Path) -> None:
    native = Fake({"data": []})
    wrapped = transport.RecordingClient(native, make_engine(f"sqlite:///{tmp_path / 'absent.db'}"))
    assert asyncio.run(wrapped.request_json("/api/stock/NVDA/info")) is native.response
    assert len(native.calls) == 1


def test_write_failure_does_not_change_response_or_leak_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    native = Fake({"data": []})
    wrapped = transport.RecordingClient(native, make_engine(f"sqlite:///{tmp_path / 'absent.db'}"), clock=lambda: T)
    assert asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"})) is native.response
    assert transport.STATE["status"] == "failed" and "SQL" not in caplog.text


def test_timed_out_recording_does_not_accumulate_threads_or_change_response(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    original = storage.append

    def slow(*args: Any, **kwargs: Any) -> str:
        started.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "append", slow)
    native = Fake({"data": []})
    wrapped = transport.RecordingClient(native, engine, clock=lambda: T, timeout_seconds=0.01)

    async def run() -> None:
        assert await wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"}) is native.response
        assert started.is_set()
        assert await wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"}) is native.response
        assert transport.STATE["error"] == "writer_busy"
        release.set()
        for _ in range(100):
            if not transport._LOCK.locked():
                break
            await asyncio.sleep(0.01)
        assert not transport._LOCK.locked()

    asyncio.run(run())
    assert len(native.calls) == 2


def test_simultaneous_conflicting_captures_are_unknown(engine: Engine) -> None:
    append(engine, {"data": [article()]})
    append(engine, {"data": []})
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2)).reason == "news_capture_timing_ambiguous"


def test_faulty_research_clock_does_not_prevent_original_call(engine: Engine) -> None:
    native = Fake({"data": []})
    wrapped = transport.RecordingClient(native, engine, clock=lambda: datetime(2026,11,4))
    assert asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"})) is native.response
    assert len(native.calls) == 1 and transport.STATE["error"] == "physical_clock_invalid"


def test_clock_failure_during_upstream_error_preserves_original_error(engine: Engine) -> None:
    failure = RuntimeError("private")
    native = Fake({}, failure)
    times = iter((T,))
    wrapped = transport.RecordingClient(native, engine, clock=lambda: next(times))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"}))
    assert caught.value is failure


def test_evidence_binds_exact_application_check_and_cached_identity(engine: Engine) -> None:
    check = T - timedelta(seconds=3)
    storage.append(engine, "NVDA", {"data": [article()]}, T-timedelta(seconds=1), T,
                   application_checked_at=check, clock=lambda: T+timedelta(seconds=1))
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2), check_at=check).flag is True
    # A cached reuse keeps the original legacy checked_at, not the later cycle.
    assert storage.evidence(engine, "NVDA", T+timedelta(minutes=2), check_at=check).flag is True
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2), check_at=T).flag is None


def test_recording_failure_restart_cannot_bind_old_success_to_new_check(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    old_check = T-timedelta(seconds=3)
    storage.append(engine, "NVDA", {"data": [article()]}, T-timedelta(seconds=1), T,
                   application_checked_at=old_check, clock=lambda: T+timedelta(seconds=1))
    native = Fake({"data": [article()]})
    original = storage.append

    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("private write failure")

    monkeypatch.setattr(storage, "append", fail)
    wrapped = transport.RecordingClient(native, engine, clock=lambda: T+timedelta(seconds=5), application_checked_at=T+timedelta(seconds=4))
    assert asyncio.run(wrapped.request_json("/api/news/headlines", params={"ticker": "NVDA"})) is native.response
    monkeypatch.setattr(storage, "append", original)
    storage._FAILURES.clear()  # Simulate process restart: durable exact identity still protects.
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=6), check_at=T+timedelta(seconds=4)).flag is None
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=6), check_at=old_check).flag is True


def test_unbound_or_tampered_application_check_is_unknown(engine: Engine) -> None:
    append(engine, {"data": [article()]})
    assert storage.evidence(engine, "NVDA", T+timedelta(seconds=2), check_at=T).flag is None
    key = storage.append(engine, "NVDA", {"data": [article()]}, T+timedelta(seconds=2), T+timedelta(seconds=3),
                         application_checked_at=T, clock=lambda: T+timedelta(seconds=4))
    with engine.begin() as c:
        c.execute(update(storage.Packet).where(storage.Packet.key == key).values(application_checked_at=T-timedelta(seconds=1)))
    result = storage.evidence(engine, "NVDA", T+timedelta(seconds=5), check_at=T-timedelta(seconds=1))
    assert result.flag is None and result.reason == "physical_receipt_hash_mismatch"
