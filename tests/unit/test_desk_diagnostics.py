"""Failure remains failed during cooldown; public counters contain no raw data."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from webapp.live_alpha import diagnostics, store, uw
from webapp.live_alpha.job import load_costs, run_cycle

from tests.unit.test_live_alpha_job import BOARD, SETTINGS, T0, FakeUW, _seed
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesNotFoundError,
)


@pytest.mark.asyncio
async def test_failed_news_cooldown_and_recovery_preserve_failed_state(tmp_path: Path) -> None:
    from webapp.board.signals import BoardSignalReader

    engine = _seed(f"sqlite:///{tmp_path / 'news.db'}")
    client = FakeUW(news_error=UnusualWhalesAuthError("HTTP 403: private-vendor-body"))
    try:
        reader = BoardSignalReader(engine=engine)
        first = await run_cycle(engine, reader, client, SETTINGS, BOARD, load_costs(), T0)
        second = await run_cycle(engine, reader, client, SETTINGS, BOARD, load_costs(), T0 + timedelta(minutes=5))
        assert sum(path == uw.NEWS_PATH for path, _ in client.calls) == 1
        assert first.snapshot["news_errors"] == second.snapshot["news_errors"] == {"http_403": 1}
        assert "private-vendor-body" not in str(second.snapshot)
        assert second.status == "degraded"
        assert "kontrol edilemedi" in second.snapshot["cards"][0]["news_evidence"]
        client.news_error = None
        third = await run_cycle(engine, reader, client, SETTINGS, BOARD, load_costs(), T0 + timedelta(minutes=15))
        assert sum(path == uw.NEWS_PATH for path, _ in client.calls) == 2
        assert third.snapshot["news_errors"] == {} and third.status == "ok"
        assert len(store.papers(engine)) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("code", [401, 403, 404, 422])
def test_http_category_retains_only_the_status(code: int) -> None:
    exc = UnusualWhalesNotFoundError("sensitive", status_code=code) if code in (404, 422) else UnusualWhalesAuthError(f"HTTP {code}: sensitive")
    detail = diagnostics.safe_http_detail(exc)
    assert "sensitive" not in detail
    assert diagnostics.news_kind(detail) == f"http_{code}"


@pytest.mark.parametrize(("detail", "category"), [
    ("UW 404/422: istek reddedildi", "auth_or_request"),
    ("UW günlük istek limiti", "daily_quota"), ("günlük kota reddi", "daily_quota"),
    ("UW 429 hız sınırı", "rate_limit"), ("beklenmeyen yanıt biçimi", "payload"),
    ("UW geçici hata (TimeoutError)", "transport"), ("vendor-secret", "other"),
])
def test_old_and_new_failure_categories_are_bounded(detail: str, category: str) -> None:
    assert diagnostics.news_kind(detail) == category


def test_summary_ignores_non_allowlisted_keys() -> None:
    assert "secret" not in diagnostics.news_message({"http_403": 2, "secret": 5})


def test_lab_exclusions_match_audits_and_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from webapp import main
    from webapp.alpha_lab import engine as lab

    from tests.unit._webapp_auth import set_web_auth
    from tests.unit.test_alpha_lab_engine import NOW, SCAN, seeded

    fixture = seeded.__wrapped__(tmp_path)
    engine, _capture = next(fixture)
    original = dict(lab.STATE)
    try:
        result = lab.observe(engine, SCAN, NOW)
        assert result["excluded_by_reason"] == {"capture_warmup": 2}
        assert result["flow_quality_counts"] == {"multileg": 0, "invalid": 0, "conflicts": 0, "unknown_flags": 0}
        assert "X" not in str(result) and "Y" not in str(result)
        monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
        response = TestClient(main.app).get("/laboratuvar", headers=set_web_auth(monkeypatch))
        assert response.status_code == 200
        assert "Adaylar hangi koşullarda eleniyor?" in response.text
        assert "Henüz kesintisiz veri penceresi yok: 2 hisse" in response.text
    finally:
        lab.STATE.clear()
        lab.STATE.update(original)
        next(fixture, None)
