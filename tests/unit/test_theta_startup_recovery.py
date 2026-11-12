"""A transient startup database failure must not strand device enrollment."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from webapp import main


@pytest.mark.asyncio
async def test_theta_tables_retry_without_blocking_app_lifespan(monkeypatch):
    marker = object()
    completed = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    attempts = []
    steps = []
    delays = []
    original_sleep = asyncio.sleep

    def flaky(engine):
        assert engine is marker
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("synthetic startup database outage")

    def last(engine):
        assert engine is marker
        steps.append("automatic")
        event_loop.call_soon_threadsafe(completed.set)

    async def pause(seconds):
        delays.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(main, "_load_board_profiles", lambda: None)
    monkeypatch.setattr(main, "live_config_from_env", lambda: None)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=marker))
    monkeypatch.setattr(main.theta_storage, "ensure", flaky)
    monkeypatch.setattr(main.theta_pairing, "ensure", lambda engine: steps.append("pairing"))
    monkeypatch.setattr(main.theta_devices, "ensure", lambda engine: steps.append("devices"))
    monkeypatch.setattr(main.theta_automatic, "ensure", last)
    monkeypatch.setattr(main.asyncio, "sleep", pause)
    async with main._lifespan(main.app):
        assert not completed.is_set()  # The HTTP lifespan starts before storage recovers.
        await asyncio.wait_for(completed.wait(), timeout=5)
    assert len(attempts) == 2
    assert delays == [30]
    assert steps == ["pairing", "devices", "automatic"]
