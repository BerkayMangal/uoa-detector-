"""Phase 5.27: automatic peers on every row of the Hisse panosu, and the rows hand baskets add."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.baskets import BasketSettings
from webapp.board.daily_close import AlfaDailyClose, ClosePoint, ensure_daily_close_tables
from webapp.live_alpha import job as live_job

from tests.unit.test_board_peers import _world
from tests.unit.test_live_alpha_job import T0, FakeUW, _cycle, _seed
from tests.unit.test_live_board_baskets import _basket_cfg, _seed_closes

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

STAMP = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)


def _no_baskets() -> BasketSettings:
    return BasketSettings.model_validate({
        "basket_version": "t", "window_sessions": 5, "min_members_with_data": 3,
        "lag_flag_pct": 3.0, "min_basket_move_pct": 2.0, "baskets": [],
    })


def _store(engine: Engine, closes: dict[str, tuple[ClosePoint, ...]]) -> None:
    from sqlalchemy.orm import Session

    ensure_daily_close_tables(engine)
    with Session(engine) as s:
        for ticker, points in closes.items():
            for point in points:
                s.merge(AlfaDailyClose(ticker=ticker, day=point.day, close=point.close, fetched_at=STAMP))
        s.commit()


def _world_with_nvda() -> dict[str, tuple[ClosePoint, ...]]:
    world = _world(lag_last=5)
    world["NVDA"] = world.pop("A")
    return world


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'live.db'}")


def test_a_row_outside_every_basket_gets_automatic_peers(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _store(engine, _world_with_nvda())
    monkeypatch.setattr(live_job, "load_basket_settings", _no_baskets)
    row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"].startswith("geride: akranlarına göre %-")
    assert "akranlar " in nvda["basket"] and nvda["basket_tone"] == "amber"


def test_a_row_the_closes_cannot_place_says_no_peers_not_nothing(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_daily_close_tables(engine)  # tables exist, nothing stored
    monkeypatch.setattr(live_job, "load_basket_settings", _no_baskets)
    row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"] == "akran bulunamadı · yeterince benzer hareket eden isim yok"


def test_a_hand_basket_wins_over_automatic_peers(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _store(engine, _world_with_nvda())  # automatic peers exist for NVDA too
    monkeypatch.setattr(live_job, "load_basket_settings", lambda: _basket_cfg(("NVDA", "B", "C", "D")))
    lags = live_job._basket_lags(engine, ["NVDA"], T0)
    assert lags["NVDA"].source == "hand" and lags["NVDA"].basket_name == "Deneme"


def test_a_hand_basket_name_without_any_closes_stays_blank_instead_of_claiming_no_peers(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(engine, _world())  # closes for SPY and A..F, none for the basket
    monkeypatch.setattr(live_job, "load_basket_settings", lambda: _basket_cfg(("NVDA", "X1", "X2", "X3")))
    lags = live_job._basket_lags(engine, ["NVDA", "A"], T0)
    assert "NVDA" not in lags
    assert lags["A"].source == "auto"


def test_unreadable_peers_profile_keeps_the_hand_readings(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_closes(engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})

    def _boom() -> Any:
        raise ValueError("peers.yaml is malformed")

    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    monkeypatch.setattr(live_job, "load_peer_settings", _boom)
    with caplog.at_level("WARNING", logger="webapp.live_alpha.job"):
        lags = live_job._basket_lags(engine, ["NVDA", "OTHER"], T0)
    assert lags["NVDA"].source == "hand" and "OTHER" not in lags
    assert "automatic peers unavailable" in caplog.text


def test_unreadable_basket_profile_costs_the_whole_column_and_logs(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom() -> Any:
        raise ValueError("baskets.yaml is malformed")

    monkeypatch.setattr(live_job, "load_basket_settings", _boom)
    with caplog.at_level("WARNING", logger="webapp.live_alpha.job"):
        assert live_job._basket_lags(engine, ["NVDA"], T0) == {}
    assert "basket lags unavailable" in caplog.text


def test_hand_basket_names_get_a_row_even_without_flow(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_closes(engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    row = _cycle(engine, FakeUW(), T0)
    by_ticker = {r["ticker"]: r for r in row.snapshot["board"]}
    assert {"PEERA", "PEERB", "PEERC"} <= set(by_ticker)
    peer = by_ticker["PEERA"]
    assert peer["bet"] == "kayda değer işlem yok" and peer["price"] is None
    assert peer["basket"] is not None and "Deneme" in peer["basket"]
    assert [r["ticker"] for r in row.snapshot["board"]].index("NVDA") < [r["ticker"] for r in row.snapshot["board"]].index("PEERA")


def test_an_unreadable_basket_profile_adds_no_rows(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise ValueError("baskets.yaml is malformed")

    monkeypatch.setattr(live_job, "load_basket_settings", _boom)
    row = _cycle(engine, FakeUW(), T0)
    assert [r["ticker"] for r in row.snapshot["board"]] == ["NVDA"]
