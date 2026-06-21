"""Smoke tests for the webapp routes — every page must render (no template
crash, no broken route, no missing context key) against a real DB.

Regression net: the audit found the webapp had no route-level coverage, so a
template typo or a context-key drift could blank a page in production unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'webapp.db'}")
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests

    import webapp.main as m
    m._REPO = m._JOURNAL = m._GAMMA = None  # fresh singletons on the temp DB

    from webapp.gamma import GammaRepo
    GammaRepo().upsert("TSLA", "2026-06-20", {
        "spot": 250.0, "net_gex": 1.0, "flip": 240.0,
        "call_wall": 260.0, "put_wall": 240.0, "atm_iv": 0.4, "iv_pct": 0.9,
    })
    from webapp.journal import JournalRepo
    JournalRepo().add(
        entry_ts=datetime.now(UTC), ticker="TSLA", direction="bullish",
        instrument="call", contracts=1.0, entry_price=3.0, thesis="smoke",
    )
    yield TestClient(m.app)
    m._REPO = m._JOURNAL = m._GAMMA = None


@pytest.mark.parametrize("path", ["/", "/gamma", "/journal", "/journal/new", "/health"])
def test_every_route_renders(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_empty_dashboard_shows_empty_state(client: TestClient) -> None:
    body = client.get("/").text
    assert "No signals match" in body  # empty signal table -> friendly empty card


def test_gamma_board_shows_seeded_ticker(client: TestClient) -> None:
    body = client.get("/gamma").text
    assert "TSLA" in body
    assert "vol-selling candidate" in body  # long-gamma + IV 90pct -> sell signal


def test_journal_shows_seeded_trade(client: TestClient) -> None:
    body = client.get("/journal").text
    assert "TSLA" in body
    assert "Building sample" in body  # 0 closed -> building verdict


def test_log_trade_form_renders(client: TestClient) -> None:
    assert "Log a trade" in client.get("/journal/new").text


def test_journal_create_and_close_roundtrip(client: TestClient) -> None:
    r = client.post("/journal", data={
        "ticker": "NVDA", "direction": "bearish", "instrument": "put",
        "contracts": "2", "entry_price": "4.0", "thesis": "test",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "NVDA" in client.get("/journal").text
