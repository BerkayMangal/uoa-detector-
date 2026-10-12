"""Phase 5.27: the lag record and its forward score (contract §3.7, §3.8)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine, func, select
from webapp.board import lag_record as lr
from webapp.board.baskets import BasketLag, BasketSettings
from webapp.board.daily_close import ClosePoint
from webapp.board.db import session_factory
from webapp.board.lag_record import AlfaLagRecord, LagRecord, forward_result, forward_results
from webapp.live_alpha import job as live_job

from tests.unit.test_live_alpha_job import T0, FakeUW, _cycle, _seed
from tests.unit.test_live_board_baskets import _basket_cfg, _seed_closes

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

NOW = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
DAY = date(2026, 9, 24)
START = date(2026, 9, 16)
END = date(2026, 9, 23)


def _baskets() -> BasketSettings:
    return BasketSettings.model_validate({
        "basket_version": "t", "window_sessions": 5, "min_members_with_data": 3,
        "lag_flag_pct": 3.0, "min_basket_move_pct": 2.0,
        "baskets": [{"id": "optical", "name": "Optik ağ", "members": ["AAOI", "COHR", "LITE"]}],
    })


def _lag(**over: Any) -> BasketLag:
    base: dict[str, Any] = {
        "basket_id": "optical", "basket_name": "Optik ağ", "state": "measured",
        "start": START, "end": END, "window_sessions": 5, "members_priced": 3, "members_total": 3,
        "residual_pct": -4.0, "peers_pct": 6.0, "own_pct": 2.0, "rank": 3, "lagging": True, "leading": False,
    }
    base.update(over)
    return BasketLag(**base)


def _record(**over: Any) -> LagRecord:
    base: dict[str, Any] = {
        "day": DAY, "ticker": "COHR", "source": "hand", "basket_id": "optical", "peers": ("AAOI", "LITE"),
        "start": START, "end": END, "window_sessions": 5, "own_pct": 2.0, "peers_pct": 6.0,
        "residual_pct": -4.0, "lagging": True, "leading": False, "version": "t+t",
    }
    base.update(over)
    return LagRecord(**base)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'record.db'}")


# ---------------------------------------------------------------------------
# What becomes a record
# ---------------------------------------------------------------------------


def test_a_hand_reading_is_recorded_against_the_other_members_of_its_basket() -> None:
    records = lr.records_from(DAY, {"COHR": _lag()}, _baskets(), "t+t")
    assert len(records) == 1
    r = records[0]
    assert (r.source, r.basket_id, r.peers) == ("hand", "optical", ("AAOI", "LITE"))
    assert (r.own_pct, r.peers_pct, r.residual_pct, r.lagging, r.leading) == (2.0, 6.0, -4.0, True, False)
    assert (r.start, r.end, r.version, r.day) == (START, END, "t+t", DAY)


def test_an_automatic_reading_keeps_the_peers_it_was_measured_against() -> None:
    lag = _lag(source="auto", basket_id="auto", basket_name="Otomatik akranlar", peers=("AMD", "AVGO", "MU"))
    (r,) = lr.records_from(DAY, {"NVDA": lag}, _baskets(), "t+t")
    assert (r.source, r.basket_id, r.peers) == ("auto", "auto", ("AMD", "AVGO", "MU"))


@pytest.mark.parametrize("state", ["thin_data", "flat_basket", "no_peers"])
def test_a_reading_that_is_not_a_measured_gap_is_not_recorded(state: str) -> None:
    lag = _lag(state=state, residual_pct=None, peers_pct=None, own_pct=None, lagging=False)
    assert lr.records_from(DAY, {"COHR": lag}, _baskets(), "t+t") == []


def test_a_hand_reading_whose_basket_has_gone_from_the_profile_is_skipped() -> None:
    assert lr.records_from(DAY, {"NVDA": _lag()}, _baskets(), "t+t") == []


def test_records_come_out_in_ticker_order() -> None:
    lags = {"LITE": _lag(), "AAOI": _lag(), "COHR": _lag()}
    assert [r.ticker for r in lr.records_from(DAY, lags, _baskets(), "t+t")] == ["AAOI", "COHR", "LITE"]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_records_round_trip_through_the_table(engine: Engine) -> None:
    assert lr.append_records(engine, [_record()], now=NOW) == 1
    (back,) = lr.load_records(engine)
    assert back == _record()
    with session_factory(engine)() as s:
        stored = s.execute(select(AlfaLagRecord)).scalar_one()
    assert stored.peers == "AAOI,LITE" and stored.recorded_at is not None


def test_a_ticker_is_written_once_per_et_date_and_never_updated(engine: Engine) -> None:
    lr.append_records(engine, [_record()], now=NOW)
    later = _record(residual_pct=-9.0, end=END + timedelta(days=1))  # same date, a different reading
    assert lr.append_records(engine, [later], now=NOW + timedelta(hours=3)) == 0
    assert [r.residual_pct for r in lr.load_records(engine)] == [-4.0]


def test_the_same_observation_on_the_next_date_is_not_counted_twice(engine: Engine) -> None:
    """A weekend reads the same stored closes as Friday; the second row would double the observation."""
    lr.append_records(engine, [_record()], now=NOW)
    weekend = _record(day=DAY + timedelta(days=2))
    assert lr.append_records(engine, [weekend], now=NOW + timedelta(days=2)) == 0
    assert len(lr.load_records(engine)) == 1


def test_a_new_end_anchor_is_a_new_record(engine: Engine) -> None:
    lr.append_records(engine, [_record()], now=NOW)
    nxt = _record(day=DAY + timedelta(days=1), end=END + timedelta(days=1), start=START + timedelta(days=1))
    assert lr.append_records(engine, [nxt], now=NOW + timedelta(days=1)) == 1
    assert [r.day for r in lr.load_records(engine)] == [DAY, DAY + timedelta(days=1)]


def test_load_records_can_start_from_a_date(engine: Engine) -> None:
    lr.append_records(engine, [_record()], now=NOW)
    lr.append_records(
        engine, [_record(day=DAY + timedelta(days=3), end=END + timedelta(days=3))], now=NOW + timedelta(days=3),
    )
    assert [r.day for r in lr.load_records(engine, since=DAY + timedelta(days=1))] == [DAY + timedelta(days=3)]


def test_an_empty_batch_writes_nothing_and_creates_no_table(engine: Engine) -> None:
    assert lr.append_records(engine, [], now=NOW) == 0
    with engine.connect() as conn:
        assert not engine.dialect.has_table(conn, "alfa_lag_record")


# ---------------------------------------------------------------------------
# The forward score
# ---------------------------------------------------------------------------


def _series(prices: dict[date, float]) -> list[ClosePoint]:
    return [ClosePoint(day=d, close=c) for d, c in sorted(prices.items())]


def _days(n: int) -> list[date]:
    """END, then n later sessions on consecutive calendar days (the score counts stored sessions, not weekdays)."""
    return [END + timedelta(days=i) for i in range(n + 1)]


def _world(own: list[float], a: list[float], b: list[float]) -> dict[str, list[ClosePoint]]:
    days = _days(len(own) - 1)
    return {
        "COHR": _series({START: 100.0, **dict(zip(days, own, strict=True))}),
        "AAOI": _series({START: 100.0, **dict(zip(days, a, strict=True))}),
        "LITE": _series({START: 100.0, **dict(zip(days, b, strict=True))}),
    }


def test_the_score_is_own_return_minus_the_peer_mean_over_the_same_span() -> None:
    # END closes at 100 everywhere; one session on: own +6%, AAOI +2%, LITE +4% -> 6 - 3 = +3
    closes = _world([100, 106], [100, 102], [100, 104])
    assert forward_result(_record(), closes, 1) == pytest.approx(3.0)


def test_a_name_that_keeps_lagging_scores_negative() -> None:
    closes = _world([100, 99, 98, 97], [100, 103, 105, 106], [100, 104, 106, 108])
    assert forward_result(_record(), closes, 3) == pytest.approx(-3.0 - 7.0)


def test_the_score_is_none_until_the_closes_reach_that_far() -> None:
    closes = _world([100, 101, 102], [100, 101, 102], [100, 101, 102])
    assert forward_result(_record(), closes, 2) == pytest.approx(0.0)
    assert forward_result(_record(), closes, 3) is None


def test_the_horizons_the_contract_names_are_one_three_and_five() -> None:
    closes = _world([100, 101, 102, 103], [100, 100, 100, 100], [100, 100, 100, 100])
    assert tuple(forward_results(_record(), closes)) == (1, 3, 5)
    out = forward_results(_record(), closes)
    assert out[1] == pytest.approx(1.0) and out[3] == pytest.approx(3.0) and out[5] is None


def test_a_hole_in_a_peer_on_the_target_session_is_none_not_a_smaller_group() -> None:
    closes = _world([100, 106], [100, 102], [100, 104])
    closes["LITE"] = [p for p in closes["LITE"] if p.day != END + timedelta(days=1)]
    assert forward_result(_record(), closes, 1) is None


def test_a_peer_the_reading_never_priced_is_left_out_of_the_comparison() -> None:
    closes = _world([100, 106], [100, 102], [100, 104])
    closes["LITE"] = [p for p in closes["LITE"] if p.day != START]  # no close on the start anchor
    assert forward_result(_record(), closes, 1) == pytest.approx(4.0)  # 6 - 2, AAOI alone


def test_no_priced_peer_at_all_is_none() -> None:
    closes = _world([100, 106], [100, 102], [100, 104])
    assert forward_result(_record(peers=("GONE",)), closes, 1) is None


def test_a_missing_own_close_is_none() -> None:
    closes = _world([100, 106], [100, 102], [100, 104])
    closes["COHR"] = [p for p in closes["COHR"] if p.day != END + timedelta(days=1)]
    assert forward_result(_record(), closes, 1) is None


@pytest.mark.parametrize("sessions", [0, -1])
def test_a_non_positive_horizon_is_none(sessions: int) -> None:
    assert forward_result(_record(), _world([100, 106], [100, 102], [100, 104]), sessions) is None


def test_the_score_does_not_touch_its_inputs() -> None:
    closes = _world([100, 106], [100, 102], [100, 104])
    before = {k: list(v) for k, v in closes.items()}
    rec = _record()
    forward_result(rec, closes, 1)
    assert closes == before and rec == _record()


# ---------------------------------------------------------------------------
# Through a real cycle
# ---------------------------------------------------------------------------


@pytest.fixture
def live_engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'live.db'}")


def test_a_cycle_keeps_the_measured_reading_of_every_row(
    live_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_closes(live_engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    _cycle(live_engine, FakeUW(), T0)
    records = {r.ticker: r for r in lr.load_records(live_engine)}
    nvda = records["NVDA"]
    assert nvda.source == "hand" and nvda.basket_id == "t"
    assert nvda.peers == ("PEERA", "PEERB", "PEERC")
    assert nvda.residual_pct == pytest.approx(-10.0) and nvda.lagging is True
    assert nvda.version.startswith("t+")  # basket_version + peers_version


def test_running_the_cycle_again_adds_nothing(live_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_closes(live_engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    _cycle(live_engine, FakeUW(), T0)
    with session_factory(live_engine)() as s:
        first = s.execute(select(func.count()).select_from(AlfaLagRecord)).scalar_one()
    _cycle(live_engine, FakeUW(), T0 + timedelta(minutes=5))
    with session_factory(live_engine)() as s:
        second = s.execute(select(func.count()).select_from(AlfaLagRecord)).scalar_one()
    assert first >= 1 and second == first


def test_a_record_failure_costs_the_record_and_not_the_row(
    live_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_closes(live_engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)

    def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(live_job, "append_records", _boom)
    with caplog.at_level(logging.WARNING, logger="webapp.live_alpha.job"):
        row = _cycle(live_engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"] is not None and nvda["basket"].startswith("geride")
    assert "lag record not written" in caplog.text


def test_a_cycle_with_no_reading_writes_no_table(live_engine: Engine) -> None:
    _cycle(live_engine, FakeUW(), T0)
    with live_engine.connect() as conn:
        assert not live_engine.dialect.has_table(conn, "alfa_lag_record")
