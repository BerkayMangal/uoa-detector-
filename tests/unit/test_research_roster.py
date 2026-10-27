"""First publication bounds the complete daily roster, including concurrent workers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from webapp import tournament_forward as live
from webapp.board.db import make_engine
from webapp.calibration.model import Bar
from webapp.tournament import context, monitor, report, storage
from webapp.tournament.model import Prepared, prepare

from tests.unit.test_tournament import C, future_bars, persist_bars

NOW = datetime(2026, 10, 28, 22, tzinfo=UTC)
CONFIG = replace(C, slots=1)


def frozen() -> dict[str, Any]:
    return {
        "report_hash": "roster-fixed",
        "tickers": ["MSFT", "NVDA", "SPY"],
        "winners": [{"id": CONFIG.id, "config": asdict(CONFIG), "paper_enabled": True}],
    }


def later_member() -> tuple[Bar, ...]:
    return tuple(
        replace(
            b, ticker="MSFT", open=b.open - 10, high=b.high - 10, low=b.low - 10, close=b.close - 10
        )
        for b in future_bars()
        if b.ticker == "NVDA"
    )


@pytest.fixture(autouse=True)
def restore_status() -> Iterator[None]:
    old = dict(live.STATE)
    yield
    live.STATE.clear()
    live.STATE.update(old)


def test_partial_arrivals_never_add_or_replace_first_daily_members_and_outcomes_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'first.db'}")
    monkeypatch.setattr(report, "load", frozen)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    persist_bars(e, future_bars(), NOW)
    live.observe(e, NOW)
    first = monitor.view(e, NOW)
    assert first["total"] == 1 and first["rows"][0]["ticker"] == "NVDA"
    persist_bars(e, later_member(), NOW + timedelta(minutes=1))
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price", "flow", "flow_gamma"]})
    live.observe(e, NOW + timedelta(minutes=1))
    second = monitor.view(e, NOW + timedelta(minutes=1))
    assert second["total"] == 1 and second["rows"][0]["ticker"] == "NVDA"
    with Session(e) as ss:
        cohorts = list(ss.scalars(select(live.AlfaTournamentCohort)))
        assert len(cohorts) == 1
        member = json.loads(cohorts[0].payload_json)["members"][0]
        assert member["context"]["arms"] == ["price"] and member["runtime_version"] == live.VERSION
        assert member["runtime_hash"] == live.runtime_hash()
    entry = NOW + timedelta(days=1)
    persist_bars(
        e,
        (
            Bar("NVDA", date(2026, 10, 29), 100, 102, 98, 101, 1000),
            Bar("SPY", date(2026, 10, 29), 125, 127, 123, 126, 1000),
        ),
        entry,
    )
    live.observe(e, entry)
    assert (
        next(r for r in monitor.view(e, entry)["rows"] if r["day"] == date(2026, 10, 28))["state"]
        == "open"
    )
    end = datetime(2026, 11, 2, 22, tzinfo=UTC)
    persist_bars(
        e,
        (
            Bar("NVDA", date(2026, 11, 2), 105, 107, 103, 106, 1000),
            Bar("SPY", date(2026, 11, 2), 126, 128, 124, 127, 1000),
        ),
        end,
    )
    live.observe(e, end)
    row = next(r for r in monitor.view(e, end)["rows"] if r["day"] == date(2026, 10, 28))
    assert row["state"] == "closed" and row["result"]["pnl_usd"] > 0
    live.observe(e, end)
    assert sum(r["day"] == date(2026, 10, 28) for r in monitor.view(e, end)["rows"]) == 1


def test_concurrent_publishers_both_read_one_database_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'concurrent.db'}")
    live.ensure(e)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    a = prepare(future_bars())
    b = prepare(tuple(x for x in future_bars() if x.ticker == "SPY") + later_member())

    def publish(p: Prepared) -> tuple[dict[str, Any], ...]:
        return live.freeze_roster(e, p, CONFIG, len(p.days) - 1, NOW, "roster-fixed", {})

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(publish, [a, b]))
    assert len(results[0]) == len(results[1]) == 1
    assert results[0][0]["ticker"] == results[1][0]["ticker"]
    with Session(e) as ss:
        assert len(list(ss.scalars(select(live.AlfaTournamentCohort)))) == 1


def test_future_cohort_is_unavailable_and_late_observer_does_not_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'future.db'}")
    live.ensure(e)
    monkeypatch.setattr(report, "load", frozen)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    persist_bars(e, future_bars(), NOW)
    p = prepare(future_bars())
    members = live.freeze_roster(
        e, p, CONFIG, len(p.days) - 1, NOW + timedelta(seconds=10), "roster-fixed", {}
    )
    assert members
    live.observe(e, NOW)
    assert monitor.view(e, NOW)["total"] == 0
    live.observe(e, datetime(2026, 10, 29, 15, tzinfo=UTC))
    assert monitor.view(e, datetime(2026, 10, 29, 15, tzinfo=UTC))["total"] == 0


def test_legacy_overcap_evidence_is_preserved_but_never_extended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    monkeypatch.setattr(report, "load", frozen)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    persist_bars(e, future_bars(), NOW)
    monitor.observe(e, NOW)
    persist_bars(e, later_member(), NOW + timedelta(seconds=1))
    monitor.observe(e, NOW + timedelta(seconds=1))
    assert monitor.view(e, NOW + timedelta(seconds=1))["total"] == 2
    live.observe(e, NOW + timedelta(seconds=2))
    assert monitor.view(e, NOW + timedelta(seconds=2))["total"] == 2
    with Session(e) as ss:
        cohort = ss.scalar(select(live.AlfaTournamentCohort))
        assert (
            cohort is not None and json.loads(cohort.payload_json)["blocked_legacy_overcap"] is True
        )
        assert len(list(ss.scalars(select(storage.TournamentAudit)))) == 2


@pytest.mark.asyncio
async def test_new_runtime_failure_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "STATE", {})

    def fail(*_: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(live, "observe", fail)
    await live.run(make_engine(f"sqlite:///{tmp_path / 'fail.db'}"), NOW)
    assert live.STATE["status"] == "failed"


def test_slow_context_cannot_backdate_publication_across_the_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'slow.db'}")
    monkeypatch.setattr(report, "load", frozen)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    persist_bars(e, future_bars(), NOW)
    after_open = datetime(2026, 10, 29, 13, 30, 1, tzinfo=UTC)
    samples = iter([NOW, after_open])

    def clock() -> datetime:
        return next(samples, after_open)

    live.observe(e, NOW, publication_clock=clock)
    assert monitor.view(e, after_open)["total"] == 0
    with Session(e) as session:
        assert list(session.scalars(select(live.AlfaTournamentCohort))) == []
