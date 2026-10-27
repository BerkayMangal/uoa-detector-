"""Observed-alert economics, capture completeness and lossless source integration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from webapp.alpha_lab import archive, db, engine
from webapp.alpha_lab.flow import ObservedAlert, PollSample, coverage, measure
from webapp.alpha_lab.settings import load_settings
from webapp.board.db import session_factory

from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)
CFG = load_settings()


@pytest.fixture(autouse=True)
def clean_states() -> Any:
    old = [dict(s) for s in (archive.STATE, engine.STATE)]
    yield
    for s, previous in zip((archive.STATE, engine.STATE), old, strict=True):
        s.clear()
        s.update(previous)


def payload(**over: Any) -> dict[str, Any]:
    data = dict(
        id="id",
        option_chain="X261120C00100000",
        created_at=(NOW - timedelta(seconds=30)).isoformat(),
        type="call",
        strike="100",
        expiry="2026-11-20",
        total_premium="1000000",
        total_ask_side_prem="510000",
        total_bid_side_prem="490000",
        has_multileg=False,
        price="2.50",
        underlying_price="101",
    )
    data.update(over)
    return data


def obs(identifier: str = "a", **over: Any) -> ObservedAlert:
    return ObservedAlert(identifier, NOW, payload(**over))


def test_balanced_million_is_not_a_million_up() -> None:
    f = measure([obs()], NOW, CFG)
    assert (f.up_usd, f.down_usd, f.net_usd) == (510000, 490000, 20000)
    assert not f.qualified


@pytest.mark.parametrize(
    ("kind", "ask", "bid", "up", "down"),
    [
        ("call", "700000", "300000", 700000, 300000),
        ("put", "700000", "300000", 300000, 700000),
    ],
)
def test_both_sides_and_put_direction_are_preserved(
    kind: str, ask: str, bid: str, up: int, down: int
) -> None:
    f = measure(
        [
            obs(
                type=kind,
                option_chain=f"X261120{'C' if kind == 'call' else 'P'}00100000",
                total_ask_side_prem=ask,
                total_bid_side_prem=bid,
            )
        ],
        NOW,
        CFG,
    )
    assert (f.up_usd, f.down_usd) == (up, down)


@pytest.mark.parametrize(
    "over",
    [
        {"has_multileg": True},
        {"has_multileg": None},
        {"has_multileg": "false"},
        {"total_ask_side_prem": "NaN"},
        {"total_bid_side_prem": "-1"},
        {"total_ask_side_prem": True},
        {"total_bid_side_prem": "900000"},
        {"type": "other"},
        {"option_chain": "not-a-contract"},
        {"option_chain": "X269920C00100000"},
        {"option_chain": "X261120P00100000"},
        {"created_at": "bad"},
        {"created_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"total_ask_side_prem": "0", "total_bid_side_prem": "0"},
    ],
)
def test_ambiguous_data_cannot_become_a_clean_control(over: dict[str, Any]) -> None:
    f = measure([obs(**over)], NOW, CFG)
    assert not f.qualified and not f.clean_control


def test_multileg_premium_stays_separate() -> None:
    f = measure([obs(has_multileg=True)], NOW, CFG)
    assert f.multileg == 1 and f.multileg_usd == 1000000
    assert f.up_usd == f.down_usd == 0


def test_replay_and_asof_conflicts_are_not_double_counted() -> None:
    a = obs(total_ask_side_prem="1000000", total_bid_side_prem="0")
    b = obs(
        "b", option_chain="X261120C00105000", total_ask_side_prem="1000000", total_bid_side_prem="0"
    )
    assert measure([a, a, b], NOW, CFG).up_usd == 2000000
    changed = ObservedAlert(
        "a",
        NOW + timedelta(seconds=10),
        payload(total_ask_side_prem="0", total_bid_side_prem="1000000"),
    )
    assert measure([a, b, changed], NOW, CFG).qualified
    later = measure([a, b, changed], NOW + timedelta(seconds=10), CFG)
    assert later.conflicts == 1 and not later.clean_control
    assert (
        measure([ObservedAlert("future", NOW + timedelta(seconds=1), payload())], NOW, CFG).alerts
        == 0
    )
    assert (
        measure([obs(created_at=(NOW - timedelta(seconds=901)).isoformat())], NOW, CFG).alerts == 0
    )


def polls() -> list[PollSample]:
    return [PollSample(NOW - timedelta(seconds=900 - i * 60), False, None) for i in range(16)]


def test_coverage_requires_continuity_and_rejects_saturation_gaps() -> None:
    assert coverage(polls(), NOW, CFG)[0]
    assert coverage(polls()[1:], NOW, CFG)[1] == "capture_warmup"
    assert coverage(polls()[:5] + polls()[9:], NOW, CFG)[1] == "poll_gap"
    assert coverage(polls(), NOW + timedelta(seconds=121), CFG)[1] == "poll_stale"
    bad = [*polls()[:-1], PollSample(NOW, True, NOW - timedelta(seconds=10))]
    assert coverage(bad, NOW, CFG)[1] == "saturated_page_gap"


@pytest.mark.asyncio
async def test_real_source_capture_preserves_sides_and_revisions_before_legacy_reduction(
    tmp_path: Path,
) -> None:
    sink = archive.FlowArchive(f"sqlite:///{tmp_path / 'raw.db'}")
    record = payload()

    class Client:
        calls = 0

        async def request_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            self.calls += 1
            return {"data": [record]}

    client = Client()
    source = UnusualWhalesFlowPollSource(client, ["X"], now=lambda: NOW)  # type: ignore[arg-type]
    source.observation_sink = sink.record
    try:
        rows = await source._fetch("X")
        raw = source._to_raw_print("X", rows[0], None)
        assert raw is not None and raw.premium_paid == 1000000  # legacy baseline preserved
        await source._fetch("X")
        with session_factory(sink.engine)() as s:
            assert s.scalar(select(func.count()).select_from(db.Alert)) == 1
            row = s.scalar(select(db.Alert))
            assert row is not None
            assert json.loads(row.payload_json)["total_bid_side_prem"] == "490000"
        record["total_ask_side_prem"] = "900000"
        record["total_bid_side_prem"] = "100000"
        await source._fetch("X")
        with session_factory(sink.engine)() as s:
            assert s.scalar(select(func.count()).select_from(db.Alert)) == 2
            assert len(set(s.scalars(select(db.Alert.alert_id)))) == 1
        assert client.calls == 3  # observer does not request another endpoint
    finally:
        sink.close()


@pytest.mark.asyncio
async def test_archive_failure_and_timeout_do_not_stop_legacy_fetch() -> None:
    class Client:
        async def request_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"data": [payload()]}

    source = UnusualWhalesFlowPollSource(Client(), ["X"], now=lambda: NOW)  # type: ignore[arg-type]

    async def fail(*_: Any) -> None:
        raise RuntimeError("database unavailable")

    source.observation_sink = fail
    assert len(await source._fetch("X")) == 1

    async def hang(*_: Any) -> None:
        await asyncio.sleep(1)

    source.observation_sink = hang
    source.observation_timeout_s = 0.001
    assert len(await source._fetch("X")) == 1


@pytest.mark.asyncio
async def test_malformed_fetch_does_not_record_a_successful_empty_poll() -> None:
    class Client:
        async def request_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"data": {"bad": []}}

    calls = []

    async def callback(*args: Any) -> None:
        calls.append(args)

    source = UnusualWhalesFlowPollSource(Client(), ["X"])  # type: ignore[arg-type]
    source.observation_sink = callback
    assert await source._fetch("X") == [] and calls == []


@pytest.mark.asyncio
async def test_invalid_lab_configuration_does_not_break_the_existing_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from webapp import worker

    from tests.unit.test_live_worker_wiring import (
        test_run_live_worker_hands_pipeline_degrading_live_stages as worker_probe,
    )

    def invalid() -> Any:
        raise ValueError("bad research profile")

    monkeypatch.setattr(worker, "load_lab_settings", invalid)
    await worker_probe(monkeypatch, tmp_path)
    assert archive.STATE["status"] == "failed"
