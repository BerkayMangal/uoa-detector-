"""Phase 5.27: the hot list wired into the worker, the refresher loop, the board rows and startup (contract §3.3 to §3.6)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from webapp import worker
from webapp.board import hotlist as hl
from webapp.board.hotlist import HotFlow, HotlistSettings, load_hotlist_settings
from webapp.live_alpha import job as live_job

from tests.unit import test_alfa_refresher_jobs as rj
from tests.unit.test_board_hotlist import _cfg
from tests.unit.test_live_alpha_job import T0, FakeUW, _cycle, _seed
from uoa_detector.sources.unusual_whales.flow_alerts import FLOW_ALERTS_PATH

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

_ET = ZoneInfo("America/New_York")


def _alerts(ticker: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "ticker": ticker, "option_chain": f"{ticker}260918C00100000", "total_size": 10,
            "created_at": f"2026-09-15T15:{i:02d}:00Z", "total_premium": str(200_000 + i),
        }
        for i in range(count)
    ]


class _HotClient(rj._FakeClient):
    """The refresher's fake client, plus the market-wide flow-alerts feed."""

    def __init__(self, feed: list[dict[str, Any]]) -> None:
        super().__init__()
        self._feed = feed

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        if path == FLOW_ALERTS_PATH:
            self.calls.append((path, params))
            return {"data": list(self._feed)}
        return await super().request_json(path, params=params, method=method)


async def _run_hot_loop(
    url: str, client: rj._FakeClient, hot: HotlistSettings | None, *, now: datetime = rj._RTH,
) -> None:
    from webapp.board.refresher import board_refresh_loop

    async def _sleep(_seconds: float) -> None:
        raise rj._Stop

    with pytest.raises(rj._Stop):
        await board_refresh_loop(
            database_url=url, profile_path=rj._CALIBRATION, board_profile_path=rj._BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: rj._Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: now, sleep=_sleep, hot_flow=hot,
        )


def _stored(url: str) -> dict[str, tuple[float, int]]:
    from webapp.board.db import make_engine, session_factory

    engine = make_engine(url)
    try:
        with session_factory(engine)() as s:
            return {r.ticker: (r.premium, r.alerts) for r in s.execute(select(hl.AlfaHotFlow)).scalars()}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The refresher loop
# ---------------------------------------------------------------------------


async def test_a_market_tick_reads_the_feed_and_stores_what_it_shows(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    rj._seed_today(url)
    client = _HotClient(_alerts("AMD", 3))
    await _run_hot_loop(url, client, _cfg())
    assert any(path == FLOW_ALERTS_PATH for path, _p in client.calls)
    stored = _stored(url)
    assert stored["AMD"][1] == 3
    states = rj._states(url)
    assert {"hot_flow", "close_catchup"} <= set(states)
    assert states["hot_flow"].status == "success"


async def test_without_the_profile_the_loop_never_touches_the_feed(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    rj._seed_today(url)
    client = _HotClient(_alerts("AMD", 3))
    await _run_hot_loop(url, client, None)
    assert not any(path == FLOW_ALERTS_PATH for path, _p in client.calls)
    assert {name for name in rj._states(url)} == {"oi_confirm", "catalysts", "gamma_history"}


async def test_the_close_universe_widens_the_daily_close_job(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    rj._seed_today(url)
    from webapp.board.db import make_engine

    engine = make_engine(url)
    try:
        hl.ensure_hotlist_tables(engine)
        hl.upsert_hot_flow(engine, [HotFlow(date(2026, 9, 14), "ZZZ", 5_000_000.0, 4)], now=rj._RTH)
    finally:
        engine.dispose()
    client = _HotClient([])
    await _run_hot_loop(url, client, _cfg(), now=rj._POST_CLOSE)
    assert any("/ZZZ/ohlc/1d" in path for path, _p in client.calls)
    assert any("/SPY/ohlc/1d" in path for path, _p in client.calls)  # the board names are still covered


async def test_a_feed_that_fails_costs_the_hot_read_and_not_the_cycle(tmp_path: Path) -> None:
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesTransientError

    url = f"sqlite:///{tmp_path / 'board.db'}"
    rj._seed_today(url)
    client = rj._FakeClient(errors={"/option-trades/flow-alerts": UnusualWhalesTransientError("boom")})
    await _run_hot_loop(url, client, _cfg())
    assert any("/quote" in p or "/greek" in p or "/api/stock" in p for p, _q in client.calls)  # the cycle still ran
    assert _stored(url) == {}


# ---------------------------------------------------------------------------
# The worker's poll list
# ---------------------------------------------------------------------------


@pytest.fixture
def hot_engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'hot.db'}")
    hl.ensure_hotlist_tables(eng)
    return eng


def test_the_poll_list_is_live_tickers_plus_the_hot_names_without_repeats(hot_engine: Engine) -> None:
    today = datetime.now(_ET).date()
    hl.upsert_hot_flow(
        hot_engine,
        [HotFlow(today, "NVDA", 9e6, 5), HotFlow(today, "AMD", 8e6, 4), HotFlow(today, "TINY", 1e6, 1)],
        now=T0,
    )
    provider = worker._poll_list(["SPY", "NVDA"], hot_engine, _cfg())
    # NVDA is already in LIVE_TICKERS; TINY has fewer alerts than poll_min_alerts
    assert provider() == ["SPY", "NVDA", "AMD"]


def test_the_poll_list_with_no_hot_data_is_live_tickers_alone(hot_engine: Engine) -> None:
    assert worker._poll_list(["SPY"], hot_engine, _cfg())() == ["SPY"]


def test_an_unreadable_hot_list_keeps_the_poll_on_live_tickers_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    broken = create_engine(f"sqlite:///{tmp_path / 'no-table.db'}")  # the table was never created
    provider = worker._poll_list(["SPY", "QQQ"], broken, _cfg())
    with caplog.at_level(logging.WARNING, logger="webapp.worker"):
        assert provider() == ["SPY", "QQQ"]
        assert provider() == ["SPY", "QQQ"]
    assert caplog.text.count("hot list unreadable") == 1


# ---------------------------------------------------------------------------
# The board row
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'live.db'}")


def test_a_row_the_hot_list_follows_carries_the_flag(engine: Engine) -> None:
    hl.ensure_hotlist_tables(engine)
    today = T0.astimezone(_ET).date()
    hl.upsert_hot_flow(engine, [HotFlow(today, "NVDA", 9e6, 5)], now=T0)
    row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["hot"] is True


def test_a_row_outside_the_hot_list_is_not_tagged(engine: Engine) -> None:
    row = _cycle(engine, FakeUW(), T0)  # no hot table at all: the tag is simply absent
    assert all(r["hot"] is False for r in row.snapshot["board"])


def test_an_unreadable_hot_list_costs_the_tag_and_not_the_row(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("hotlist_v1.yaml is malformed")

    monkeypatch.setattr(live_job, "load_hotlist_settings", _boom)
    with caplog.at_level(logging.WARNING, logger="webapp.live_alpha.job"):
        row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["hot"] is False and nvda["verdict"] != "—"
    assert "hot list unavailable" in caplog.text


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", ["load_peer_settings", "load_hotlist_settings"])
def test_a_malformed_new_profile_fails_startup(monkeypatch: pytest.MonkeyPatch, loader: str) -> None:
    import webapp.main as m

    def _boom() -> Any:
        raise ValueError(f"{loader}: malformed")

    monkeypatch.setattr(m, loader, _boom)
    with pytest.raises(ValueError, match="malformed"):
        m._load_board_profiles()


def test_the_committed_profiles_validate() -> None:
    cfg = load_hotlist_settings()
    assert cfg.poll_top_n >= 1 and cfg.universe_cap >= cfg.poll_top_n
    assert cfg.excluded("spy") and not cfg.excluded("NVDA")
