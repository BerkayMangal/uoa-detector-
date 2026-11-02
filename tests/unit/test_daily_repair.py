"""Operational capture fixtures test clocks and mutation boundaries, not alpha."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.call_research import coverage
from webapp.call_research import daily_repair as repair
from webapp.call_research.storage import DailyReceipt
from webapp.history_capture.capture import CaptureStoppedError

NOW = datetime(2026, 11, 2, 10, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path / 'daily.db'}")
    monkeypatch.setattr(coverage, "targets", lambda: ("NFLX", "SPY"))
    return e


def packet(days):
    return {
        "payload": {
            "data": [
                dict(
                    date=str(d),
                    market_time="r",
                    open="100",
                    high="102",
                    low="98",
                    close="101",
                    volume="1000",
                )
                for d in days
            ]
        },
        "received_at": NOW.isoformat(),
        "quota": {"native_date": NOW.isoformat(), "count": 1, "limit": 30000},
    }


class Fetch:
    def __init__(self, body, error=None):
        self.body = body
        self.error = error
        self.calls = 0
        self.remaining = 29999

    async def get(self, path, params):
        self.calls += 1
        if self.error:
            raise self.error
        return self.body


def test_preview_needs_no_tables_and_never_mutates(engine):
    before = inspect(engine).get_table_names()
    p = repair.plan(engine, NOW)
    assert p.names == ("NFLX", "SPY") and len(p.days) == 60
    assert str(p.expected_day) == "2026-10-30"
    assert inspect(engine).get_table_names() == before


async def test_completed_bars_and_physical_receipts_are_first_write_idempotent(engine, tmp_path):
    days = repair.plan(engine, NOW).days[:4]
    body = packet((*days, NOW.date()))  # Today's unclosed bar must stay absent.
    fetch = Fetch(body)
    later = NOW + timedelta(seconds=10)
    result = await repair.repair(engine, fetch, tmp_path / "run", lambda: later)
    assert result["status"] == "complete" and result["after_ready"] == 2
    assert fetch.calls == 2
    with Session(engine) as s:
        bars = s.scalars(select(DailyBar)).all()
        receipts = s.scalars(select(DailyReceipt)).all()
        assert len(bars) == len(receipts) == 8
        assert all(r.day <= days[0] for r in bars)
        assert all(r.observed_at.replace(tzinfo=UTC) == later for r in (*bars, *receipts))
        saved = [(r.key, r.payload_json) for r in bars]
    second = Fetch(body)
    again = await repair.repair(
        engine, second, tmp_path / "again", lambda: later + timedelta(hours=1)
    )
    assert second.calls == 0 and again["status"] == "complete"
    with Session(engine) as s:
        assert [(r.key, r.payload_json) for r in s.scalars(select(DailyBar))] == saved


async def test_missing_sessions_remain_partial_not_zero(engine, tmp_path):
    days = repair.plan(engine, NOW).days[:3]
    result = await repair.repair(engine, Fetch(packet(days)), tmp_path / "run", lambda: NOW)
    assert result["status"] == "partial" and result["after_ready"] == 0
    assert coverage.view(engine, NOW).rows[0].status == "history_missing"


@pytest.mark.parametrize("damage", ["conflict", "ohlc", "date"])
async def test_rejects_corrupt_ticker_payload_before_any_bar_write(engine, tmp_path, damage):
    days = repair.plan(engine, NOW).days[:4]
    body = packet(days)
    rows = body["payload"]["data"]
    if damage == "conflict":
        rows.append(rows[0] | {"close": "100.5"})
    elif damage == "ohlc":
        rows[0]["high"] = "90"
    else:
        rows[0]["date"] = "unknown"
    result = await repair.repair(engine, Fetch(body), tmp_path / "run", lambda: NOW)
    assert result["status"] == "partial"
    assert all(r["status"] == "rejected" for r in result["rows"])
    with Session(engine) as s:
        assert s.scalars(select(DailyBar)).all() == []


async def test_auth_stop_records_budget_without_retry_or_secret_text(engine, tmp_path):
    fetch = Fetch({}, CaptureStoppedError("http_401"))
    result = await repair.repair(engine, fetch, tmp_path / "run", lambda: NOW)
    assert result["status"] == "stopped" and result["http_calls"] == 1 and fetch.calls == 1
    assert result["reason"] == "http_401"


async def test_clock_cannot_backdate_physical_source(engine, tmp_path):
    days = repair.plan(engine, NOW).days[:4]
    body = packet(days) | {"received_at": (NOW + timedelta(seconds=1)).isoformat()}
    result = await repair.repair(engine, Fetch(body), tmp_path / "run", lambda: NOW)
    assert result["status"] == "stopped" and result["reason"] == "physical_clock_invalid"
    with Session(engine) as s:
        assert s.scalars(select(DailyBar)).all() == []


async def test_old_invalid_keys_and_receipts_are_not_replaced(engine, tmp_path):
    days = repair.plan(engine, NOW).days[:4]
    await repair.repair(engine, Fetch(packet(days[:3])), tmp_path / "first", lambda: NOW)
    # Existing rows have another source price. A later source cannot rewrite them.
    body = packet(days)
    body["payload"]["data"][0]["close"] = "100.5"
    before = NOW + timedelta(hours=1)
    result = await repair.repair(engine, Fetch(body), tmp_path / "second", lambda: before)
    assert result["status"] == "partial" and sum(r["conflicts"] for r in result["rows"]) == 2
    with Session(engine) as s:
        old = s.scalars(select(DailyBar).where(DailyBar.day == days[0])).all()
        assert all(json.loads(r.payload_json)["close"] == 101 for r in old)
        receipts = s.scalars(
            select(DailyReceipt).where(DailyReceipt.key.in_([r.key for r in old]))
        ).all()
        assert all(r.observed_at.replace(tzinfo=UTC) == NOW for r in receipts)


async def test_storage_exception_is_redacted_and_run_cannot_overwrite(
    engine, tmp_path, monkeypatch
):
    days = repair.plan(engine, NOW).days[:4]

    def broken(*args):
        raise RuntimeError("database-url-and-secret-must-not-escape")

    monkeypatch.setattr(repair, "append", broken)
    dest = tmp_path / "run"
    result = await repair.repair(engine, Fetch(packet(days)), dest, lambda: NOW)
    assert result["status"] == "stopped" and "secret" not in (dest / "run.json").read_text()
    with pytest.raises(FileExistsError):
        await repair.repair(engine, Fetch(packet(days)), dest, lambda: NOW)
