"""Phase 5.2.PERF10 hotfix: the default board view is prebuilt and served from memory.

Production measured 10-29 s per ``GET /`` on 2026-09-23/24 and owners closed the
tab before it answered (HTTP 499). Pins:
  - a fresh prebuilt view is served without touching the database-bound builder,
    and the page says how old it is;
  - a view older than two refresher cycles is not served; the request rebuilds;
  - any ``run``, ``gate`` or ``pas`` parameter bypasses the prebuilt view;
  - no prebuilt view yet (first minutes after a deploy) means the request builds;
  - the age sentence passes the board's honesty guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from webapp.board.copy_tr import PREBUILT_AGE
from webapp.board.honesty import ensure_clean

from tests.unit.test_board_honesty import board, seeded  # noqa: F401  (fixtures)

if TYPE_CHECKING:
    import pytest
    from fastapi.testclient import TestClient

_NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)


def _counting_builder(m: Any, calls: list[tuple[str, str, str]]) -> Any:
    real = m._board_context

    def _wrapped(run: str, gate: str, pas: str) -> dict[str, Any]:
        calls.append((run, gate, pas))
        ctx: dict[str, Any] = real(run, gate, pas)
        return ctx

    return _wrapped


def _prime(m: Any, monkeypatch: pytest.MonkeyPatch, *, age_seconds: int) -> None:
    ctx = m._board_context("", "", "")
    monkeypatch.setattr(m, "_PRERENDERED", (_NOW - timedelta(seconds=age_seconds), ctx))
    monkeypatch.setattr(m, "_now", lambda: _NOW)


def test_fresh_prebuilt_view_is_served_without_rebuilding(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    _prime(m, monkeypatch, age_seconds=42)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    for path in ("/", "/alfa"):
        response = seeded.get(path)
        assert response.status_code == 200
        assert 'id="board-prebuilt"' in response.text
        assert "42 sn önce" in response.text
    assert calls == []


def test_stale_prebuilt_view_is_rebuilt(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    max_age = m._prerender_max_age_seconds()
    _prime(m, monkeypatch, age_seconds=max_age + 1)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    response = seeded.get("/")
    assert response.status_code == 200
    assert 'id="board-prebuilt"' not in response.text
    assert calls == [("", "", "")]


def test_boundary_age_is_still_served(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    _prime(m, monkeypatch, age_seconds=m._prerender_max_age_seconds())
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    assert seeded.get("/").status_code == 200
    assert calls == []


def test_parameters_bypass_the_prebuilt_view(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    _prime(m, monkeypatch, age_seconds=1)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    for query in ("?gate=off", "?run=live-2026-09-15", "?pas=nope"):
        response = seeded.get("/" + query)
        assert response.status_code == 200
        assert 'id="board-prebuilt"' not in response.text
    assert [c for c in calls if c != ("", "", "")] == [
        ("", "off", ""), ("live-2026-09-15", "", ""), ("", "", "nope"),
    ]


def test_no_prebuilt_view_builds_on_request(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    monkeypatch.setattr(m, "_PRERENDERED", None)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    assert seeded.get("/").status_code == 200
    assert calls == [("", "", "")]


def test_clock_running_backwards_rebuilds(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import webapp.main as m

    _prime(m, monkeypatch, age_seconds=-5)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "_board_context", _counting_builder(m, calls))
    assert seeded.get("/").status_code == 200
    assert calls == [("", "", "")]


def test_age_sentence_is_clean() -> None:
    ensure_clean(PREBUILT_AGE.format(age=7))
