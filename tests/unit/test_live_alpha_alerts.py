"""Only current stored executable plans may reach an opt-in browser alert."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from webapp import main
from webapp.board.db import make_engine
from webapp.live_alpha import alerts, job, store

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_live_alpha_job import BOARD, COSTS, SETTINGS, T0, FakeUW, _seed

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    e = make_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    store.ensure_live_tables(e)
    yield e
    e.dispose()


def card(now: datetime = NOW, instrument: str = "long_call") -> dict[str, Any]:
    return {
        "ticker": "NVDA", "opportunity_id": f"{now.date()}:NVDA:up", "policy_version": "live_alpha_v1",
        "market_mode": "LIVE", "recommendation": "BUY", "readiness": "READY", "direction": "up",
        "decision_as_of": now.isoformat(), "instrument": {"preferred": instrument},
        "stock_plan": {"entry_ref": 101.0, "stop": 98.0, "target": 107.0},
        "options": [{"kind": instrument, "readiness": "READY", "legs": [
            {"option_symbol": "NVDA261120C00100000", "fetched_at": now.isoformat()},
            *([{"option_symbol": "NVDA261120C00105000", "fetched_at": now.isoformat()}] if instrument == "bull_call_debit" else []),
        ]}],
    }


def write(e: Engine, c: dict[str, Any] | None = None, now: datetime = NOW, **overrides: Any) -> store.ScanRow:
    snapshot = {"cards": [c or card(now)], "spot_age_seconds": {"NVDA": 0}, "flow_current": True}
    snapshot.update(overrides.pop("snapshot", {}))
    row = store.ScanRow("scan", now, now, "LIVE", "ok", "run", snapshot)
    row = replace(row, **overrides)
    store.write_scan(e, row, "live_alpha_v1")
    return row


@pytest.mark.parametrize("instrument", ["stock", "long_call", "bull_call_debit"])
def test_feed_labels_actual_preferred_instrument_and_stable_identity(engine: Engine, instrument: str) -> None:
    c = card(instrument=instrument)
    write(engine, c)
    signal = alerts.feed(engine, NOW + timedelta(seconds=1)).signals[0]
    assert signal.instrument == instrument
    assert signal.label == {"stock": "HİSSE ALIM", "long_call": "CALL", "bull_call_debit": "CALL SPREAD"}[instrument]
    assert signal.underlying_ref == 101 and signal.underlying_stop == 98
    assert len(signal.contracts) == (0 if instrument == "stock" else 2 if instrument == "bull_call_debit" else 1)
    # Price, quote and recommendation record churn are not new opportunities.
    c["rec_id"] = "another-record"
    c["stock_plan"]["entry_ref"] = 101.2
    c["options"][0]["legs"][0]["fetched_at"] = (NOW + timedelta(seconds=2)).isoformat()
    write(engine, c, scan_id="next", finished_at=NOW + timedelta(seconds=2))
    assert alerts.feed(engine, NOW + timedelta(seconds=3)).signals[0].key == signal.key


@pytest.mark.parametrize("changes", [
    {"recommendation": "WATCH"}, {"recommendation": "CONDITIONAL_BUY"},
    {"recommendation": "BEARISH_SETUP", "direction": "down"}, {"readiness": "TRIGGER_PENDING"},
    {"readiness": "QUOTE_PENDING"}, {"market_mode": "PREMARKET"}, {"instrument": {"preferred": "none"}},
    {"decision_as_of": None}, {"decision_as_of": (NOW + timedelta(seconds=2)).isoformat()},
    {"decision_as_of": NOW.replace(tzinfo=None).isoformat()}, {"stock_plan": None},
    {"opportunity_id": "2026-10-26:NVDA:up"},
])
def test_non_executable_and_unavailable_cards_never_alert(engine: Engine, changes: dict[str, Any]) -> None:
    write(engine, card() | changes)
    assert not alerts.feed(engine, NOW + timedelta(seconds=1)).signals


@pytest.mark.parametrize("age", [None, -1, True, 901, 899])
def test_spot_age_is_required_and_keeps_aging_after_the_scan(engine: Engine, age: object) -> None:
    write(engine, snapshot={"spot_age_seconds": {"NVDA": age}})
    assert not alerts.feed(engine, NOW + timedelta(seconds=2)).signals


@pytest.mark.parametrize("stamp", [None, (NOW - timedelta(seconds=901)).isoformat(), (NOW + timedelta(seconds=2)).isoformat()])
def test_selected_call_leg_must_have_a_current_quote(engine: Engine, stamp: object) -> None:
    c = card()
    c["options"][0]["legs"][0]["fetched_at"] = stamp
    write(engine, c)
    assert not alerts.feed(engine, NOW + timedelta(seconds=1)).signals


def test_stale_unselected_option_does_not_suppress_a_valid_stock_plan(engine: Engine) -> None:
    c = card(instrument="stock")
    c["options"][0]["legs"][0]["fetched_at"] = None
    write(engine, c, status="degraded")  # failed news is not a failed market-relative BUY
    assert alerts.feed(engine, NOW + timedelta(seconds=1)).signals[0].label == "HİSSE ALIM"


def test_no_scan_future_scan_failed_scan_and_previous_session_are_explicit(engine: Engine) -> None:
    assert alerts.feed(engine, NOW).status == "no_scan"
    write(engine, status="failed")
    assert alerts.feed(engine, NOW).status == "unavailable"
    write(engine, scan_id="future", started_at=NOW + timedelta(seconds=1), finished_at=NOW + timedelta(seconds=1))
    assert alerts.feed(engine, NOW).status == "unavailable"
    # Latest scan remains yesterday when the new session opens.
    assert not alerts.feed(engine, NOW + timedelta(days=1)).signals


def test_scan_and_flow_freshness_and_session_close_are_hard_limits(engine: Engine) -> None:
    write(engine)
    assert alerts.feed(engine, NOW + timedelta(seconds=900)).status == "stale"
    assert alerts.feed(engine, NOW.replace(hour=20)).status == "not_live"
    write(engine, scan_id="old-flow", snapshot={"flow_current": False})
    assert alerts.feed(engine, NOW).status == "unavailable"


def test_early_close_caps_the_notification_expiry(engine: Engine) -> None:
    now = datetime(2026, 11, 27, 17, 59, 30, tzinfo=UTC)
    write(engine, now=now)
    assert alerts.feed(engine, now).signals[0].expires_at == now + timedelta(seconds=30)
    assert alerts.feed(engine, now + timedelta(seconds=30)).status == "not_live"


def test_feed_and_both_scripts_are_private_noindex_and_never_cached(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    write(engine)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    for path in ("/api/canli/uyarilar", "/assets/live-alerts.js", "/assets/live-desk.js"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "noindex" in response.headers["x-robots-tag"]
    assert client.get("/api/canli/uyarilar", headers=headers).json()["signals"][0]["instrument"] == "long_call"


def test_real_cycle_to_feed_uses_no_additional_vendor_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return T0 if tz is None else T0.astimezone(tz)

    class TightQuotes(FakeUW):
        async def request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            response = await super().request_json(path, **kwargs)
            if path.endswith("/option-contracts"):
                for row in response["data"]:
                    row.update(nbbo_bid="0.28", nbbo_ask="0.30")
            return response

    e = _seed(f"sqlite:///{tmp_path / 'real-cycle.db'}")
    monkeypatch.setattr(job, "datetime", Clock)
    client = TightQuotes()
    try:
        from webapp.board.signals import BoardSignalReader

        asyncio.run(job.run_cycle(e, BoardSignalReader(engine=e), client, SETTINGS, BOARD, COSTS, T0))
        before = copy.deepcopy(client.calls)
        data = alerts.feed(e, T0 + timedelta(seconds=1))
        assert data.status == "current" and data.signals[0].instrument == "long_call"
        assert client.calls == before
    finally:
        e.dispose()


def test_invalid_optional_alert_configuration_does_not_break_the_desk(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_config() -> Any:
        raise ValueError("invalid optional alert settings")

    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main.live_alerts, "settings", bad_config)
    response = TestClient(main.app).get("/", headers=set_web_auth(monkeypatch))
    assert response.status_code == 200
    assert "Uyarı ayarları okunamadı" in response.text
    assert 'id="alerts-toggle"' not in response.text
