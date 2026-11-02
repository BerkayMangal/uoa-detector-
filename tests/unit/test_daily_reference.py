"""A missing exact prior session must not become a multi-session entry baseline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from webapp.alpha_lab.db import digest, dumps
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.call_research.storage import DailyReceipt
from webapp.live_alpha import inputs
from webapp.live_alpha.daily_reference import previous, supplement
from webapp.live_alpha.job import _fresh_spot
from webapp.ohlc import RegularBar

from uoa_detector.live_alpha.calendar import MarketMode
from uoa_detector.live_alpha.model import CheckState

NOW = datetime(2026, 11, 2, 14, 10, tzinfo=UTC)
OLD = RegularBar(date(2026, 10, 29), 100, 102, 98, 100)
NEW = RegularBar(date(2026, 10, 30), 100, 103, 99, 102)


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'reference.db'}")
    DailyBar.__table__.create(e)
    DailyReceipt.__table__.create(e)
    return e


def insert(engine, *, seen=NOW, wrong_hash=False, invalid=False, unsettled=False):
    body = dumps(
        dict(open=100.0, high=90.0 if invalid else 103.0, low=99.0, close=102.0, volume=1000.0)
    )
    key = digest(("NVDA", NEW.day))
    with Session(engine) as s:
        s.add(
            DailyBar(
                key=key,
                ticker="NVDA",
                day=NEW.day,
                observed_at=datetime(2026, 10, 30, 19, tzinfo=UTC) if unsettled else seen,
                payload_json=body,
            )
        )
        s.add(
            DailyReceipt(
                key=key, observed_at=seen, payload_hash="wrong" if wrong_hash else digest(body)
            )
        )
        s.commit()


def context(bars):
    return inputs.price_context(
        "NVDA",
        inputs.SpotRead(103.0, NOW, NOW.date()),
        bars,
        None,
        atr_period=1,
        atr_min_sessions=1,
    )


def test_monday_reference_is_friday_and_unknown_calendar_stays_unknown():
    assert previous(NOW.date()) == date(2026, 10, 30)
    assert previous(date(2026, 11, 27)) == date(2026, 11, 25)
    assert previous(date(2028, 1, 4)) is None
    bad = context((OLD,))
    assert bad.state is CheckState.FAILED and "2026-10-30" in bad.blocker
    assert context((OLD, NEW)).state is CheckState.CHECKED_FOUND


def test_verified_missing_day_supplements_without_any_write(engine):
    insert(engine)
    writes = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")):
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    bars = supplement(engine, {"NVDA": (OLD,)}, {"NVDA": NOW.date()}, NOW)
    assert bars["NVDA"] == (OLD, NEW) and writes == []
    assert context(bars["NVDA"]).prev_close_day == NEW.day


@pytest.mark.parametrize("damage", ["hash", "ohlc", "unsettled", "future"])
def test_unverified_or_physically_unavailable_data_is_not_a_reference(engine, damage):
    insert(
        engine,
        seen=NOW + timedelta(minutes=1) if damage == "future" else NOW,
        wrong_hash=damage == "hash",
        invalid=damage == "ohlc",
        unsettled=damage == "unsettled",
    )
    bars = supplement(engine, {"NVDA": (OLD,)}, {"NVDA": NOW.date()}, NOW)
    assert bars["NVDA"] == (OLD,) and context(bars["NVDA"]).state is CheckState.FAILED


def test_existing_legacy_row_cannot_be_replaced_by_a_different_source(engine):
    insert(engine)
    original = RegularBar(NEW.day, 100, 104, 98, 101)
    assert supplement(engine, {"NVDA": (OLD, original)}, {"NVDA": NOW.date()}, NOW)["NVDA"] == (
        OLD,
        original,
    )


def test_failed_benchmark_never_provides_relative_strength(engine, monkeypatch):
    monkeypatch.setattr(
        inputs,
        "latest_spots",
        lambda *_: {name: inputs.SpotRead(103.0, NOW, NOW.date()) for name in ("NVDA", "SPY")},
    )
    monkeypatch.setattr(
        inputs, "load_bars_by_ticker", lambda *_: {"NVDA": (OLD, NEW), "SPY": (OLD,)}
    )
    prices = inputs.price_contexts(
        engine, ["NVDA"], "SPY", atr_period=1, atr_min_sessions=1, max_age_seconds=900, asof=NOW
    )
    assert prices["SPY"].state is CheckState.FAILED
    assert prices["NVDA"].benchmark_move is None and prices["NVDA"].relative is None


def test_fresh_paper_exit_does_not_require_an_entry_baseline():
    price = context((OLD,))
    assert price.state is CheckState.FAILED
    ctx = SimpleNamespace(
        now=NOW,
        session=SimpleNamespace(mode=MarketMode.LIVE),
        settings=SimpleNamespace(price=SimpleNamespace(spot_max_age_seconds=900)),
    )
    assert _fresh_spot(price, ctx) == 103.0


def test_fresh_fetch_of_a_previous_session_is_not_current_price_context(engine, monkeypatch):
    monkeypatch.setattr(
        inputs,
        "latest_spots",
        lambda *_: {
            name: inputs.SpotRead(103.0, NOW, date(2026, 10, 30)) for name in ("NVDA", "SPY")
        },
    )
    monkeypatch.setattr(
        inputs, "load_bars_by_ticker", lambda *_: {"NVDA": (OLD, NEW), "SPY": (OLD, NEW)}
    )
    values = inputs.price_contexts(
        engine, ["NVDA"], "SPY", atr_period=1, atr_min_sessions=1, max_age_seconds=900, asof=NOW
    )
    assert values["NVDA"].state is CheckState.FAILED
    assert "fiyat kaynak seansı" in values["NVDA"].blocker
    assert values["NVDA"].benchmark_move is None


def test_paper_exit_rejects_a_fresh_fetch_of_a_previous_session():
    price = replace(context((OLD, NEW)), source_session=date(2026, 10, 30))
    ctx = SimpleNamespace(
        now=NOW,
        session=SimpleNamespace(mode=MarketMode.LIVE),
        settings=SimpleNamespace(price=SimpleNamespace(spot_max_age_seconds=900)),
    )
    assert _fresh_spot(price, ctx) is None
