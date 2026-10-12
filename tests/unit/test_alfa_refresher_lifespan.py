"""Phase 5.2.A2: the app lifespan supervises the board refresher.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("webapp/board/refresher.py
runs under _supervise in the app lifespan"). Like the live worker and the gamma
loop, it starts only when ``live_config_from_env()`` returns a config.

Pins:
  - with a live config, ``board_refresh_loop`` is started with the live
    ``database_url`` and nothing else;
  - without one, it is never started.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
import webapp.main as m
from fastapi.testclient import TestClient


async def _forever(**_kwargs: Any) -> None:
    await asyncio.Event().wait()


def _live_config(database_url: str) -> dict[str, object]:
    return {
        "tickers": ["SPY"],
        "database_url": database_url,
        "poll_interval_s": 60.0,
        "min_premium": Decimal(25000),
    }


def test_lifespan_starts_the_board_refresher_with_the_live_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict[str, Any]] = []

    async def _board(**kwargs: Any) -> None:
        started.append(kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(m, "live_config_from_env", lambda: _live_config("sqlite:///live.db"))
    monkeypatch.setattr(m, "run_live_worker", _forever)
    monkeypatch.setattr(m, "gamma_refresh_loop", _forever)
    monkeypatch.setattr(m, "board_refresh_loop", _board)

    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200
        for _ in range(100):
            if started:
                break
            client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]

    # Phase 5.26: the loop also receives the basket-member provider, so basket names get a daily close.
    # Phase 5.27: and the hot-list profile, which switches the market-wide hot list on.
    assert started == [{
        "database_url": "sqlite:///live.db",
        "close_extra": m.basket_close_tickers,
        "hot_flow": m.load_hotlist_settings(),
    }]


def test_lifespan_does_not_start_the_board_refresher_without_live_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[dict[str, Any]] = []

    async def _board(**kwargs: Any) -> None:
        called.append(kwargs)

    monkeypatch.setattr(m, "live_config_from_env", lambda: None)
    monkeypatch.setattr(m, "board_refresh_loop", _board)

    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200
        client.portal.call(asyncio.sleep, 0.01)  # type: ignore[union-attr]

    assert called == []
