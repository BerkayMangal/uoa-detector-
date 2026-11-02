"""Original first calls survive later WAIT updates without becoming current entries."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from webapp.board.db import make_engine
from webapp.desk.calls import today
from webapp.live_alpha import store
from webapp.opening import storage
from webapp.opening.model import Advice, clock

from tests.unit.test_opening_desk import source

NOW = datetime(2026, 11, 2, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    value = make_engine(f"sqlite:///{tmp_path / 'calls.db'}")
    store.ensure_live_tables(value)
    storage.ensure(value)
    return value


def publish(engine: Engine, at: datetime, action: str) -> None:
    storage.publish(engine, source(at, at.isoformat()), at, clock(at), (
        Advice(ticker="MSFT", action=action, headline=f"MSFT {action}", reasons=("Synthetic test",)),
    ))


def test_first_call_remains_after_wait_and_repeated_buy(engine: Engine) -> None:
    first = NOW - timedelta(minutes=20)
    publish(engine, first, "BUY")
    publish(engine, NOW - timedelta(minutes=10), "BUY")
    publish(engine, NOW - timedelta(minutes=5), "WAIT")
    rows = today(engine, NOW)
    assert rows is not None and len(rows) == 1
    assert rows[0].at == first and rows[0].headline == "MSFT BUY"


def test_prior_day_future_and_wait_are_not_today_calls(engine: Engine) -> None:
    publish(engine, NOW - timedelta(days=3), "BUY")
    publish(engine, NOW - timedelta(minutes=1), "WAIT")
    publish(engine, NOW + timedelta(minutes=1), "BUY")
    assert today(engine, NOW) == ()


def test_missing_storage_is_unknown_not_zero(tmp_path: Path) -> None:
    assert today(make_engine(f"sqlite:///{tmp_path / 'missing.db'}"), NOW) is None
