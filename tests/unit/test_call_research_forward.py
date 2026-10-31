"""Prospective first-writer/availability guarantees; every price is synthetic."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.orm import Session
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.calibration.storage import ensure as ensure_bars
from webapp.call_research import coverage, forward
from webapp.call_research.settings import settings
from webapp.call_research.storage import DailyReceipt, ResearchCohort, ResearchResult, ensure

DAYS = (
    date(2026, 10, 28),
    date(2026, 10, 29),
    date(2026, 10, 30),
    date(2026, 11, 2),
    date(2026, 11, 3),
    date(2026, 11, 4),
    date(2026, 11, 5),
)
NOW = datetime(2026, 11, 2, 22, tzinfo=UTC)


def prices(engine, through: int = 3, omit: str = "") -> None:
    rows = []
    for i, day in enumerate(DAYS[: through + 1]):
        for ticker in (*coverage.prospective_stocks(), *settings().benchmarks):
            if ticker == omit and i == through:
                continue
            close = 100.0 - i if ticker == "SPY" else 100.0 if ticker == "QQQ" else 100.0 + i
            rows.append(
                dict(
                    key=digest((ticker, day)),
                    ticker=ticker,
                    day=day,
                    observed_at=datetime.combine(day, datetime.min.time(), UTC).replace(
                        hour=21, minute=30
                    ),
                    payload_json=dumps(
                        dict(open=close, high=close + 2, low=close - 2, close=close, volume=1000)
                    ),
                )
            )
    append(engine, cast(Table, DailyBar.__table__), rows)
    append(
        engine,
        cast(Table, DailyReceipt.__table__),
        [
            dict(key=r["key"], payload_hash=digest(r["payload_json"]), observed_at=r["observed_at"])
            for r in rows
        ],
    )


@pytest.fixture
def engine(tmp_path: Path):
    e = make_engine(f"sqlite:///{tmp_path / 'forward.db'}")
    ensure_bars(e)
    ensure(e)
    yield e
    e.dispose()


def counts(engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        c = list(session.scalars(select(ResearchCohort)))
        r = list(session.scalars(select(ResearchResult)))
    return len(c), sum(x.kind == "receipt" for x in r), sum(x.kind == "outcome" for x in r)


def test_partial_arrivals_cannot_freeze_an_incomplete_ranking(engine) -> None:
    prices(engine, omit="AAPL")
    forward.observe(engine, NOW, lambda: NOW)
    assert forward.STATE["status"] == "partial_coverage" and counts(engine) == (0, 0, 0)
    prices(engine)
    forward.observe(engine, NOW, lambda: NOW)
    assert counts(engine) == (1, 1, 0)
    with Session(engine) as session:
        cohort = session.scalar(select(ResearchCohort))
        assert cohort is not None
        payload = json.loads(cohort.payload_json)
    assert all(len(m) <= settings().max_positions for m in payload["members"].values())
    assert payload["members"]["delay_control"] == [] and payload["members"]["retest"] == []


def test_concurrent_publishers_keep_one_roster_and_one_receipt(engine) -> None:
    prices(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: forward.observe(engine, NOW, lambda: NOW), range(2)))
    assert counts(engine) == (1, 1, 0)
    with Session(engine) as session:
        cohort = session.scalar(select(ResearchCohort))
        assert cohort is not None
        original = cohort.payload_json
    later = datetime(2026, 11, 3, 15, tzinfo=UTC)
    forward.observe(engine, later, lambda: later)
    with Session(engine) as session:
        cohort = session.scalar(select(ResearchCohort))
        assert cohort is not None
        assert cohort.payload_json == original
    assert forward.STATE["status"] == "cohort_recorded"


def test_late_physical_clock_cannot_advance_an_earlier_input_snapshot(engine) -> None:
    prices(engine, through=4)
    late = datetime(2026, 11, 3, 22, tzinfo=UTC)
    forward.observe(engine, NOW, lambda: late)
    assert forward.STATE["status"] == "deadline_missed" and counts(engine) == (0, 0, 0)


def test_a_commit_crossing_the_opening_never_acquires_a_valid_publication_receipt(engine) -> None:
    prices(engine)
    before = datetime(2026, 11, 3, 14, 29, 59, tzinfo=UTC)
    after = datetime(2026, 11, 3, 14, 30, 1, tzinfo=UTC)
    moments = iter((before, after))
    forward.observe(engine, before, lambda: next(moments))
    assert forward.STATE["status"] == "publication_late" and counts(engine) == (1, 1, 0)
    prices(engine, through=6)
    final = datetime(2026, 11, 5, 22, tzinfo=UTC)
    forward.observe(engine, final, lambda: final)
    with Session(engine) as session:
        old = session.scalar(select(ResearchCohort).where(ResearchCohort.day == DAYS[3]))
        assert old is not None
        outcomes = list(
            session.scalars(
                select(ResearchResult).where(
                    ResearchResult.cohort_key == old.key, ResearchResult.kind == "outcome"
                )
            )
        )
    assert outcomes == []


def test_exact_matured_reference_units_are_idempotent_and_never_use_partial_prices(engine) -> None:
    prices(engine)
    forward.observe(engine, NOW, lambda: NOW)
    final = datetime(2026, 11, 5, 22, tzinfo=UTC)
    prices(engine, through=6, omit="QQQ")
    forward.observe(engine, final, lambda: final)
    assert counts(engine)[2] == 0
    prices(engine, through=6)
    forward.observe(engine, final, lambda: final)
    before = counts(engine)
    assert before[2] > 0
    forward.observe(engine, final, lambda: final)
    assert counts(engine) == before
    with Session(engine) as session:
        rows = session.scalars(select(ResearchResult).where(ResearchResult.kind == "outcome")).all()
    for row in rows:
        result = json.loads(row.payload_json)
        assert result["entry_day"] == "2026-11-03" and result["exit_day"] == "2026-11-05"
        assert result["scope"] == "independent_daily_stock_reference_unit"
        assert result["spy_excess_pp"] is not None and result["qqq_excess_pp"] is not None


def test_future_capture_timestamps_and_nonfinal_prices_stay_unavailable(engine) -> None:
    prices(engine)
    future = datetime(2026, 11, 3, 22, tzinfo=UTC)
    assert coverage.observed(engine, NOW.replace(hour=21, minute=0))[0]
    # A future session inserted with an impossibly early availability must not
    # become a valid feature later, even when that date eventually arrives.
    row = dict(
        key="invalid-future",
        ticker="NFLX",
        day=date(2026, 11, 9),
        observed_at=NOW,
        payload_json=dumps(dict(open=100, high=101, low=99, close=100)),
    )
    append(engine, cast(Table, DailyBar.__table__), [row])
    got, _ = coverage.observed(engine, datetime(2026, 11, 10, 22, tzinfo=UTC))
    assert not next(p.bar for p in got if p.bar.day == date(2026, 11, 9)).valid
    assert all(p.available_at <= future for p in coverage.observed(engine, future)[0])


def test_legacy_job_timestamp_requires_later_physical_receipt_before_strict_use(engine) -> None:
    from webapp.call_research import availability

    row = dict(
        key="legacy-only",
        ticker="NFLX",
        day=DAYS[3],
        observed_at=NOW - timedelta(minutes=20),
        payload_json=dumps(dict(open=100, high=102, low=99, close=101)),
    )
    append(engine, cast(Table, DailyBar.__table__), [row])
    assert coverage.observed(engine, NOW)[0]
    assert coverage.observed(engine, NOW, verified=True)[0] == ()
    received = NOW + timedelta(minutes=1)
    assert availability.adopt(engine, coverage.targets(), NOW, lambda: received) == 1
    assert coverage.observed(engine, NOW, verified=True)[0] == ()
    proven = coverage.observed(engine, received, verified=True)[0]
    assert len(proven) == 1 and proven[0].verified and proven[0].available_at == received
    assert (
        availability.adopt(
            engine, coverage.targets(), received, lambda: received + timedelta(hours=1)
        )
        == 0
    )
    assert coverage.observed(engine, received, verified=True)[0] == proven
