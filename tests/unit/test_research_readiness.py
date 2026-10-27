"""Coverage is evidence availability, not a monitor heartbeat or a buy signal."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from sqlalchemy import Table, inspect
from sqlalchemy.orm import Session
from webapp import research_readiness as ready
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import make_engine
from webapp.calibration import storage as bar_storage
from webapp.tournament import model, report
from webapp.tournament import storage as decisions
from webapp.tournament_forward import AlfaTournamentCohort, ensure

from tests.unit.test_tournament import C, fake_report, future_bars, persist_bars

NOW = datetime(2026, 10, 28, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ready, "STATE", {})
    monkeypatch.setattr(ready, "_LAST_CHECK", None)
    yield


@pytest.mark.parametrize(
    ("moment", "day"),
    [
        ("2026-10-28T19:59:59+00:00", "2026-10-27"),
        ("2026-10-28T20:00:00+00:00", "2026-10-28"),
        ("2026-11-26T22:00:00+00:00", "2026-11-25"),
        ("2026-11-27T17:59:59+00:00", "2026-11-25"),
        ("2026-11-27T18:00:00+00:00", "2026-11-27"),
        ("2026-11-28T20:00:00+00:00", "2026-11-27"),
        ("2026-11-02T20:59:59+00:00", "2026-10-30"),
        ("2026-11-02T21:00:00+00:00", "2026-11-02"),
        ("2028-01-05T22:00:00+00:00", None),
    ],
)
def test_settled_session_handles_early_close_holiday_weekend_and_dst(
    moment: str, day: str | None
) -> None:
    assert ready.settled_day(datetime.fromisoformat(moment)) == (
        date.fromisoformat(day) if day else None
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        ready.settled_day(datetime(2026, 10, 28))


def test_empty_diagnostics_do_not_create_tables_or_report_zero_market_signals(
    tmp_path: Path,
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    before = inspect(e).get_table_names()
    snap = ready.build(e, NOW)
    assert snap.status == "no_bars" and snap.covered == 0
    assert snap.latest_day is None and snap.deadline is None
    assert snap.families[0].reasons["current_bar_missing"] == 69
    assert inspect(e).get_table_names() == before


def test_future_observations_unsettled_bars_and_other_names_do_not_enter_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "load", fake_report)
    e = make_engine(f"sqlite:///{tmp_path / 'future.db'}")
    bar_storage.ensure(e)
    # Deliberately inject impossible rows to test the reader's own finality guard.
    data = {"open": 100, "high": 102, "low": 98, "close": 101, "volume": 1000}
    before_close = datetime(2026, 10, 28, 19, tzinfo=UTC)
    with Session(e) as session:
        for ticker, day, observed in (
            ("SPY", date(2026, 10, 28), before_close),  # current, still unsettled
            ("SPY", date(2026, 10, 27), before_close + timedelta(seconds=1)),
            ("OUTSIDE", date(2026, 10, 27), before_close),
        ):
            session.add(
                bar_storage.DailyBar(
                    key=digest((ticker, day)),
                    ticker=ticker,
                    day=day,
                    observed_at=observed,
                    payload_json=dumps(data),
                )
            )
        session.commit()
    snap = ready.build(e, before_close)
    assert snap.status == "no_bars" and snap.covered == 0
    assert snap.expected_day == date(2026, 10, 27)
    assert ready.build(e, before_close + timedelta(seconds=2)).covered == 1


def test_complete_history_is_eligible_but_missing_middle_session_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "load", fake_report)
    e = make_engine(f"sqlite:///{tmp_path / 'good.db'}")
    persist_bars(e, future_bars(), NOW)
    snap = ready.build(e, NOW)
    assert snap.status == "eligible_unpublished" and snap.covered == snap.expected == 2
    assert snap.families[0].reasons["eligible"] == 1
    assert snap.deadline == datetime(2026, 10, 29, 13, 30, tzinfo=UTC)
    assert ready.build(e, snap.deadline).status == "deadline_missed"
    assert not ready.build(e, snap.deadline).deadline_open
    gap = make_engine(f"sqlite:///{tmp_path / 'gap.db'}")
    source = future_bars()
    missing = sorted({b.day for b in source})[-5]
    persist_bars(
        gap, tuple(b for b in source if not (b.ticker == "NVDA" and b.day == missing)), NOW
    )
    g = ready.build(gap, NOW)
    assert g.covered == 2 and g.status == "insufficient_history"
    assert g.families[0].reasons["history_missing"] == 1


def test_missing_reference_session_and_global_warmup_are_not_sufficient_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "load", fake_report)
    source = future_bars()
    ds = sorted({b.day for b in source})
    for label, bars in (
        ("warmup", tuple(b for b in source if b.day >= ds[-10])),
        ("spy_gap", tuple(b for b in source if not (b.ticker == "SPY" and b.day == ds[-6]))),
    ):
        e = make_engine(f"sqlite:///{tmp_path / (label + '.db')}")
        persist_bars(e, bars, NOW)
        s = ready.build(e, NOW)
        assert s.status == "insufficient_history" and s.families[0].reasons["history_missing"] == 1


def test_stale_price_day_and_waiting_start_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "load", fake_report)
    e = make_engine(f"sqlite:///{tmp_path / 'stale.db'}")
    persist_bars(e, tuple(b for b in future_bars() if b.day < NOW.date()), NOW - timedelta(days=1))
    assert ready.build(e, NOW).status == "stale_bars"
    old = ready.build(e, NOW - timedelta(days=1))
    assert old.status == "waiting_start" and old.latest_day == date(2026, 10, 27)


def test_complete_no_setup_is_distinct_from_an_unobserved_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = replace(C, threshold=1000)
    monkeypatch.setattr(
        report,
        "load",
        lambda: fake_report() | {"winners": [{"config": asdict(c), "paper_enabled": True}]},
    )
    e = make_engine(f"sqlite:///{tmp_path / 'no_setup.db'}")
    persist_bars(e, future_bars(), NOW)
    assert ready.build(e, NOW).status == "no_setup"
    monkeypatch.setattr(
        report,
        "load",
        lambda: (
            fake_report()
            | {
                "tickers": ["MISSING", "NVDA", "SPY"],
                "winners": [{"config": asdict(c), "paper_enabled": True}],
            }
        ),
    )
    s = ready.build(e, NOW)
    assert s.status == "partial_coverage" and s.families[0].reasons["current_bar_missing"] == 1


def test_every_reason_reconciles_and_eligibility_matches_the_original_mask() -> None:
    p = model.prepare(future_bars())
    names = ("ABSENT", "NVDA")
    for c in model.grid():
        counts = ready.reasons(p, c, names, p.days[-1])
        assert sum(counts.values()) == len(names)
        assert counts["eligible"] == int(model.mask(p, c)[-1, p.tickers.index("NVDA")])
    c = replace(C, volume=1)
    p.volume[-1, p.tickers.index("NVDA")] = np.nan
    assert ready.reasons(p, c, names, p.days[-1])["volume_unavailable"] == 1


def test_first_roster_and_arm_counts_are_frozen_asof_and_config_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "load", fake_report)
    e = make_engine(f"sqlite:///{tmp_path / 'cohort.db'}")
    persist_bars(e, future_bars(), NOW)
    ensure(e)
    rh = fake_report()["report_hash"]
    with Session(e) as session:
        session.add(
            AlfaTournamentCohort(
                key=digest((rh, C.id, NOW.date())),
                config_id=C.id,
                day=NOW.date(),
                observed_at=NOW,
                payload_json=dumps({"members": ["NVDA"]}),
            )
        )
        session.commit()
    append(
        e,
        cast(Table, decisions.TournamentDecision.__table__),
        [
            dict(
                key=str(i),
                ticker="NVDA",
                family=C.family,
                arm=arm,
                day=NOW.date(),
                observed_at=observed,
                payload_json=dumps({"report_hash": rh, "config_id": cid}),
            )
            for i, (arm, observed, cid) in enumerate(
                (
                    ("price", NOW, C.id),
                    ("flow", NOW + timedelta(seconds=1), C.id),
                    ("flow_oi", NOW, "another-config"),
                )
            )
        ],
    )
    s = ready.build(e, NOW)
    assert s.status == "cohort_recorded" and s.families[0].cohort_members == 1
    assert s.families[0].arm_counts == dict.fromkeys(ready.settings().arms, 0) | {"price": 1}
    early = ready.publication(e, NOW - timedelta(seconds=1), NOW.date(), C, rh)
    assert early[0] == "unfrozen" and early[1] is None and not sum(early[2].values())
    assert ready.build(e, s.deadline).status == "cohort_recorded"
    assert ready.build(e, NOW + timedelta(seconds=2)).families[0].arm_counts["flow"] == 1


@pytest.mark.asyncio
async def test_background_check_has_cadence_and_failure_clears_stale_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'job.db'}")
    await ready.run(e, NOW)
    assert ready.STATE["status"] == "no_bars"
    checked = ready.STATE["at"]
    await ready.run(e, NOW + timedelta(seconds=1))
    assert ready.STATE["at"] == checked

    def fail(*_: Any) -> ready.Snapshot:
        raise RuntimeError("PRIVATE VENDOR PAYLOAD OR SECRET")

    monkeypatch.setattr(ready, "build", fail)
    await ready.run(e, NOW + timedelta(seconds=301))
    assert ready.public_summary() == {
        "status": "failed",
        "at": (NOW + timedelta(seconds=301)).isoformat(),
    }
    assert ready.view(e, NOW) is None


@pytest.mark.asyncio
async def test_timed_out_read_only_thread_cannot_later_publish_stale_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'timeout.db'}")
    release, done = threading.Event(), threading.Event()
    original = ready.build
    s = ready.settings().model_copy(update={"monitor_timeout_seconds": 0.01})
    monkeypatch.setattr(ready, "settings", lambda: s)

    def blocked(*args: Any) -> ready.Snapshot:
        try:
            assert release.wait(2)
            return original(*args)
        finally:
            done.set()

    monkeypatch.setattr(ready, "build", blocked)
    try:
        await ready.run(e, NOW)
        assert {"status": "failed", "at": NOW.isoformat()} == ready.STATE
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 3)
    assert {"status": "failed", "at": NOW.isoformat()} == ready.STATE


def test_public_health_summary_cannot_include_private_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webapp.live_alpha import job
    from webapp.main import health

    monkeypatch.setattr(job, "LAST_CYCLE", {"status": "failed", "at": NOW.isoformat()})

    monkeypatch.setattr(
        ready,
        "STATE",
        {
            "status": "no_bars",
            "at": NOW.isoformat(),
            "covered": 0,
            "tickers": ["PRIVATE"],
            "payload": {"raw": "PRIVATE"},
            "error": "PRIVATE",
            "families_detail": ["PRIVATE"],
        },
    )
    assert "PRIVATE" not in json.dumps(health())
    assert health()["research_data"] == {"status": "no_bars", "at": NOW.isoformat(), "covered": 0}
    monkeypatch.setattr(job, "LAST_CYCLE", {})
    assert "research_data" not in health()


@pytest.mark.asyncio
async def test_readiness_runs_even_when_the_signal_cycle_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from webapp import gamma
    from webapp.live_alpha import job

    from tests.unit.test_live_alpha_job import FakeUW

    seen = []

    async def fail(*_: Any, **__: Any) -> None:
        seen.append("cycle")
        raise RuntimeError("synthetic unavailable source")

    async def check(*args: Any) -> None:
        seen.append("readiness")
        await ready.run(*args)

    monkeypatch.setattr(job, "run_cycle", fail)
    monkeypatch.setattr(job, "run_readiness", check)
    monkeypatch.setattr(job, "LAST_CYCLE", {})
    monkeypatch.setattr(gamma, "GammaRepo", lambda *_: SimpleNamespace(latest=lambda: None))
    client = FakeUW()
    await job.live_alpha_loop(
        database_url=f"sqlite:///{tmp_path / 'failed_cycle.db'}",
        client_factory=lambda: client,
        clock=lambda: NOW,
        max_cycles=1,
    )
    assert seen == ["cycle", "readiness"]
    assert job.LAST_CYCLE["status"] == "failed" and ready.STATE["status"] == "no_bars"
    assert not client.calls


def test_real_private_report_contains_readiness_and_stays_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from webapp import main

    from tests.unit._webapp_auth import set_web_auth

    e = make_engine(f"sqlite:///{tmp_path / 'route.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/stratejiler").status_code == 401
    response = client.get("/stratejiler", headers=headers)
    assert response.status_code == 200
    assert "Veri eksik mi, sinyal mi yok?" in response.text
    assert "SPY referans verisi bekleniyor" in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
