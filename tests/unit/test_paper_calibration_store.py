from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine, inspect
from webapp.paper_calibration import store

DAY = date(2026, 11, 23)
AT = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
STUDY = "paper_test"


def case(
    opportunity_id: str = "nvda",
    ticker: str = "NVDA",
    at: datetime = AT,
    *,
    active: bool = True,
) -> store.CaseRecord:
    return store.CaseRecord(opportunity_id, ticker, DAY, at, '{"case":true}', active)


def test_report_reads_before_worker_setup() -> None:
    engine = create_engine("sqlite://")

    assert store.active_tickers(engine, STUDY) == ()
    assert store.detail_inputs(engine, STUDY, "nvda") == (None, ())
    assert store.report_inputs(engine, STUDY, None, 1, 10) == store.ReportInputs(
        None, (), (), 0, False
    )
    assert inspect(engine).get_table_names() == []

    store.ensure(engine)
    store.ensure(engine)
    assert set(inspect(engine).get_table_names()) == {
        "alfa_paper_calibration_clock",
        "alfa_paper_calibration_case",
        "alfa_paper_calibration_event",
    }
    assert store.report_inputs(engine, STUDY, None, 1, 10) == store.ReportInputs(
        None, (), (), 0, True
    )


def test_failed_scan_rolls_back_clock_case_and_event() -> None:
    engine = create_engine("sqlite://")
    store.ensure(engine)

    with (
        pytest.raises(RuntimeError, match="observer interrupted"),
        store.begin_scan(engine, STUDY, "first", AT) as scan,
    ):
        assert scan is not None
        scan.put(case())
        scan.add_event(store.EventRecord("nvda", "first", AT, '{"first":true}'))
        raise RuntimeError("observer interrupted")

    assert store.report_inputs(engine, STUDY, None, 1, 10).latest_at is None
    assert store.detail_inputs(engine, STUDY, "nvda") == (None, ())
    with store.begin_scan(engine, STUDY, "first", AT) as scan:
        assert scan is not None
        assert scan.active_cases() == ()
        assert scan.existing(DAY) == frozenset()
        scan.put(case())
        scan.add_event(store.EventRecord("nvda", "first", AT, '{"first":true}'))

    assert store.active_tickers(engine, STUDY) == ("NVDA",)
    assert store.detail_inputs(engine, STUDY, "nvda") == (
        case(),
        (store.EventRecord("nvda", "first", AT, '{"first":true}'),),
    )


def test_study_scoping_utc_and_case_updates() -> None:
    engine = create_engine("sqlite://")
    store.ensure(engine)
    offset = timezone(timedelta(hours=-5))
    second = AT + timedelta(minutes=5)

    with store.begin_scan(engine, STUDY, "first", AT.astimezone(offset)) as scan:
        assert scan is not None
        scan.put(case(at=AT.astimezone(offset)))
        scan.add_event(store.EventRecord("nvda", "first", AT.astimezone(offset), "{}"))
        assert scan.existing(DAY) == frozenset({"nvda"})
        assert scan.active_cases() == (case(),)

    with store.begin_scan(engine, STUDY, "second", second) as scan:
        assert scan is not None
        assert scan.active_cases() == (case(),)
        scan.put(case(active=False))
        scan.add_event(store.EventRecord("nvda", "second", second, '{"next":true}'))
        scan.put(case("spy", "SPY", second))
        scan.put(case("nvda-two", "NVDA", second))

    for key, at in (("first", AT), ("out-of-order", AT), ("equal", second), ("second", second)):
        with store.begin_scan(engine, STUDY, key, at) as scan:
            assert scan is None
    with store.begin_scan(engine, "independent", "first", AT) as scan:
        assert scan is not None
        assert scan.existing(DAY) == frozenset()
        scan.put(case("nvda", "AAPL"))

    result = store.report_inputs(engine, STUDY, "NVDA", 2, 1)
    assert result.latest_at == second
    assert result.latest_at.tzinfo is UTC
    assert result.cases == (case(active=False),)
    assert result.total == 2
    assert len(result.all_cases) == 3
    assert store.active_tickers(engine, STUDY) == ("NVDA", "SPY")
    assert store.active_tickers(engine, "independent") == ("AAPL",)
    found, events = store.detail_inputs(engine, STUDY, "nvda")
    assert found == case(active=False)
    assert found.published_at.tzinfo is UTC
    assert [event.scan_id for event in events] == ["first", "second"]
    assert all(event.at.tzinfo is UTC for event in events)


def test_concurrent_observers_accept_same_scan_once(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'observers.db'}")
    barrier = Barrier(4)

    def enter(_: int) -> bool:
        barrier.wait(timeout=10)
        store.ensure(engine)
        with store.begin_scan(engine, STUDY, "first", AT) as scan:
            if scan is None:
                return False
            scan.put(case())
            scan.add_event(store.EventRecord("nvda", "first", AT, "{}"))
            return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(enter, range(4))) == 1
    found, events = store.detail_inputs(engine, STUDY, "nvda")
    assert found == case()
    assert len(events) == 1
