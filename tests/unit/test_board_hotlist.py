"""Phase 5.27: the market-wide hot list, its storage, readers and the two daily jobs (contract §3.3 to §3.6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from webapp.board import hotlist as hl
from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
from webapp.board.daily_jobs import JobContext
from webapp.board.db import session_factory
from webapp.board.hotlist import HotFlow, HotlistSettings, aggregate_alerts, load_hotlist_settings

from tests.unit.test_board_daily_close import _path, _spy_payload
from tests.unit.test_board_daily_jobs import _SETTINGS, _Reader
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
)
from uoa_detector.sources.unusual_whales.flow_alerts import FLOW_ALERTS_PAGE_LIMIT, FLOW_ALERTS_PATH

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

NOW = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)  # 09:00 ET, a Monday
TODAY = date(2026, 10, 12)


def _cfg(**over: object) -> HotlistSettings:
    base: dict[str, object] = {
        "hotlist_version": "t", "poll_top_n": 3, "poll_min_alerts": 2, "universe_sessions": 5,
        "universe_min_alerts": 2, "universe_cap": 10, "pages_per_tick": 3,
        "job_time_et": "08:30", "catchup_time_et": "09:00", "catchup_max": 2,
        "exclude": ["SPY", "SPX"],
    }
    base.update(over)
    return HotlistSettings.model_validate(base)


def _alert(ticker: str, when: str, premium: str = "100000", chain: str | None = None, size: int = 10) -> dict[str, Any]:
    return {
        "ticker": ticker, "option_chain": chain or f"{ticker}261016C00100000", "created_at": when,
        "total_premium": premium, "total_size": size,
    }


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'hot.db'}")
    hl.ensure_hotlist_tables(eng)
    return eng


def _put(engine: Engine, day: date, ticker: str, premium: float, alerts: int) -> None:
    hl.upsert_hot_flow(engine, [HotFlow(day, ticker, premium, alerts)], now=NOW)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_the_shipped_profile_loads_with_the_contract_numbers() -> None:
    cfg = load_hotlist_settings()
    assert (cfg.poll_top_n, cfg.poll_min_alerts, cfg.universe_sessions) == (12, 2, 20)
    assert (cfg.universe_min_alerts, cfg.universe_cap, cfg.pages_per_tick) == (2, 250, 3)
    assert (cfg.job_time_et, cfg.catchup_time_et, cfg.catchup_max) == ("08:30", "09:00", 80)
    assert cfg.excluded("spy") and cfg.excluded("SPXW") and not cfg.excluded("COHR")


@pytest.mark.parametrize("over", [{"job_time_et": "8:30"}, {"pages_per_tick": 0}, {"poll_min_alerts": 0}, {"bogus": 1}])
def test_the_profile_rejects_bad_values(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _cfg(**over)


def test_a_non_mapping_profile_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "h.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_hotlist_settings(path)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_premium_and_alerts_are_summed_per_et_session_and_ticker() -> None:
    rows = [
        _alert("AAA", "2026-10-12T14:00:00Z", "100000", chain="A1"),
        _alert("AAA", "2026-10-12T15:00:00Z", "50000", chain="A2"),
        _alert("BBB", "2026-10-12T15:00:00Z", "70000"),
    ]
    got = {(f.day, f.ticker): (f.premium, f.alerts) for f in aggregate_alerts(rows, _cfg())}
    assert got == {(TODAY, "AAA"): (150000.0, 2), (TODAY, "BBB"): (70000.0, 1)}


def test_the_session_date_is_eastern_time_not_utc() -> None:
    late = _alert("AAA", "2026-10-13T02:00:00Z")  # 22:00 ET on the 12th
    assert aggregate_alerts([late], _cfg())[0].day == TODAY


def test_an_alert_seen_on_two_overlapping_pages_counts_once() -> None:
    dup = _alert("AAA", "2026-10-12T14:00:00Z", chain="A1")
    assert aggregate_alerts([dup, dict(dup)], _cfg())[0].alerts == 1


def test_excluded_products_and_unusable_rows_are_ignored() -> None:
    rows = [
        _alert("SPY", "2026-10-12T14:00:00Z"),
        _alert("spx", "2026-10-12T14:00:00Z"),
        {"ticker": "", "created_at": "2026-10-12T14:00:00Z", "total_premium": "1"},
        {"ticker": "BAD", "created_at": "not a time", "total_premium": "1"},
        {"ticker": "BAD", "created_at": "2026-10-12T14:00:00Z", "total_premium": "abc"},
        {"ticker": "BAD", "created_at": "2026-10-12T14:00:00Z", "total_premium": "-5"},
        {"ticker": "BAD", "created_at": "2026-10-12T14:00:00Z"},
    ]
    assert aggregate_alerts(rows, _cfg()) == []


# ---------------------------------------------------------------------------
# Storage and readers
# ---------------------------------------------------------------------------


def test_the_stored_value_is_the_maximum_observed_never_a_sum(engine: Engine) -> None:
    _put(engine, TODAY, "AAA", 500.0, 4)
    _put(engine, TODAY, "AAA", 300.0, 3)  # a later, smaller window must not lower it
    assert hl.upsert_hot_flow(engine, [HotFlow(TODAY, "AAA", 900.0, 2)], now=NOW) == 1  # premium rose
    with session_factory(engine)() as s:
        row = s.get(hl.AlfaHotFlow, (TODAY, "AAA"))
        assert row is not None and (row.premium, row.alerts) == (900.0, 4)


def test_the_poll_list_is_the_top_names_of_the_newest_session_by_premium(engine: Engine) -> None:
    for ticker, premium, alerts in (("AAA", 500, 5), ("BBB", 900, 3), ("CCC", 100, 2), ("DDD", 800, 2), ("ONE", 9999, 1), ("SPY", 99999, 9)):
        _put(engine, TODAY, ticker, float(premium), alerts)
    _put(engine, TODAY - timedelta(days=1), "OLD", 1e9, 9)
    assert hl.load_hot_tickers(engine, TODAY, _cfg()) == ("BBB", "DDD", "AAA")  # ONE: too few alerts; SPY: excluded


def test_before_the_open_the_list_is_the_last_session_that_has_data(engine: Engine) -> None:
    _put(engine, date(2026, 10, 9), "AAA", 500.0, 3)
    assert hl.load_hot_tickers(engine, TODAY, _cfg()) == ("AAA",)
    assert hl.load_hot_tickers(engine, date(2026, 10, 8), _cfg()) == ()  # nothing at or before that day


def test_a_zero_poll_size_follows_nothing_and_an_empty_table_follows_nothing(engine: Engine) -> None:
    _put(engine, TODAY, "AAA", 500.0, 3)
    assert hl.load_hot_tickers(engine, TODAY, _cfg(poll_top_n=0)) == ()


def test_the_universe_spans_recent_sessions_filters_and_caps(engine: Engine) -> None:
    _put(engine, date(2026, 10, 12), "AAA", 500.0, 3)
    _put(engine, date(2026, 10, 9), "BBB", 700.0, 2)
    _put(engine, date(2026, 10, 8), "BBB", 100.0, 5)
    _put(engine, date(2026, 10, 7), "CCC", 600.0, 1)  # one alert: below universe_min_alerts
    _put(engine, date(2026, 9, 1), "OLD", 9e9, 9)  # outside the last five stored sessions
    for day in (date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 2)):
        _put(engine, day, "FILL", 1.0, 2)
    cfg = _cfg(universe_sessions=5)
    assert hl.load_universe(engine, TODAY, cfg) == ("BBB", "AAA", "FILL")
    assert hl.load_universe(engine, TODAY, _cfg(universe_sessions=5, universe_cap=2)) == ("BBB", "AAA")
    assert hl.load_universe(engine, date(2026, 8, 1), cfg) == ()


def test_the_close_universe_reader_never_raises(tmp_path: Path) -> None:
    bare = create_engine(f"sqlite:///{tmp_path / 'bare.db'}")  # no table at all
    assert hl.hot_close_tickers(bare, NOW, _cfg()) == ()


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------


class _Feed:
    """Serves scripted pages for the market-wide feed and one payload for any ohlc/1d path."""

    def __init__(self, pages: list[list[dict[str, Any]] | Exception]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path == FLOW_ALERTS_PATH:
            page = self.pages.pop(0) if self.pages else []
            if isinstance(page, Exception):
                raise page
            return {"data": page}
        return _spy_payload()


def _full_page(start_minute: int) -> list[dict[str, Any]]:
    base = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
    return [
        _alert("AAA", (base - timedelta(seconds=start_minute * 600 + i)).isoformat().replace("+00:00", "Z"),
               chain=f"C{start_minute}-{i}")
        for i in range(FLOW_ALERTS_PAGE_LIMIT)
    ]


async def test_the_refresh_asks_without_a_ticker_and_stores_the_session(engine: Engine) -> None:
    feed = _Feed([[_alert("AAA", "2026-10-12T14:00:00Z"), _alert("BBB", "2026-10-12T14:01:00Z", chain="B")]])
    result = await hl.refresh_hot_flow(feed, engine, _cfg(), now=NOW)
    assert result.status == "ok" and result.alerts == 2 and result.pages == 1 and result.written == 2
    path, params = feed.calls[0]
    assert path == FLOW_ALERTS_PATH and params == {"limit": FLOW_ALERTS_PAGE_LIMIT}  # no ticker_symbol: market-wide
    assert hl.load_universe(engine, TODAY, _cfg(universe_min_alerts=1)) == ("AAA", "BBB")


async def test_full_pages_are_walked_back_in_time_up_to_the_page_bound(engine: Engine) -> None:
    feed = _Feed([_full_page(0), _full_page(1), _full_page(2), _full_page(3)])
    result = await hl.refresh_hot_flow(feed, engine, _cfg(pages_per_tick=3), now=NOW)
    assert result.pages == 3 and len(feed.calls) == 3 and result.alerts == 3 * FLOW_ALERTS_PAGE_LIMIT
    cursors = [params["older_than"] for _, params in feed.calls[1:] if params]
    assert len(cursors) == 2 and cursors[1] < cursors[0]


async def test_a_short_page_ends_the_walk(engine: Engine) -> None:
    feed = _Feed([_full_page(0)[:150], _full_page(1)])
    assert (await hl.refresh_hot_flow(feed, engine, _cfg(), now=NOW)).pages == 1


async def test_a_cursor_that_cannot_move_back_ends_the_walk(engine: Engine) -> None:
    same = _full_page(0)
    feed = _Feed([same, list(same), list(same)])
    assert (await hl.refresh_hot_flow(feed, engine, _cfg(pages_per_tick=3), now=NOW)).pages == 2


async def test_ordinary_uw_trouble_is_one_degraded_refresh_not_an_exception(engine: Engine) -> None:
    for error in (UnusualWhalesRateLimitError("429"), UnusualWhalesAuthError("HTTP 422 bad parameter")):
        result = await hl.refresh_hot_flow(_Feed([error]), engine, _cfg(), now=NOW)
        assert result.status == "degraded" and result.written == 0
    assert (await hl.refresh_hot_flow(_Feed([UnusualWhalesNotFoundError("404", status_code=404)]), engine, _cfg(), now=NOW)).status == "no_data"


@pytest.mark.parametrize("error", [UnusualWhalesDailyLimitError("daily_request_limit_hit"), UnusualWhalesAuthError("HTTP 401")])
async def test_a_daily_limit_or_a_failed_key_still_propagates(engine: Engine, error: Exception) -> None:
    with pytest.raises(type(error)):
        await hl.refresh_hot_flow(_Feed([error]), engine, _cfg(), now=NOW)


# ---------------------------------------------------------------------------
# The two daily jobs
# ---------------------------------------------------------------------------


def _ctx(engine: Engine, client: _Feed, *, tickers: tuple[str, ...] = (), extra: tuple[str, ...] = ()) -> JobContext:
    return JobContext(
        client=client, engine=engine, sessions=session_factory(engine), settings=_SETTINGS, now=NOW,
        tickers=tickers, reader=_Reader(), close_extra=extra,
    )


def test_the_two_jobs_run_on_the_profile_clock() -> None:
    jobs = hl.hot_daily_jobs(_cfg())
    assert [j.name for j in jobs] == ["hot_flow", "close_catchup"]
    assert [j.et_time.strftime("%H:%M") for j in jobs] == ["08:30", "09:00"]
    assert all(j.trading_day_only for j in jobs)


async def test_the_hot_flow_job_refreshes_and_reports(engine: Engine) -> None:
    job = hl.hot_daily_jobs(_cfg())[0]
    detail = await job.run(_ctx(engine, _Feed([[_alert("AAA", "2026-10-12T14:00:00Z")]])))
    assert detail == "status=ok alerts=1 pages=1 written=1"


async def test_the_catch_up_fetches_only_names_with_no_close_and_respects_its_cap(engine: Engine) -> None:
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as s:
        s.add(AlfaDailyClose(ticker="HAVE", day=date(2026, 10, 9), close=10.0, fetched_at=NOW))
        s.commit()
    feed = _Feed([])
    job = hl.hot_daily_jobs(_cfg(catchup_max=2))[1]
    detail = await job.run(_ctx(engine, feed, tickers=("HAVE", "T1"), extra=("X1", "X2", "T1")))
    fetched = [p for p, _ in feed.calls if p != FLOW_ALERTS_PATH]
    assert _path("X1") in fetched and _path("X2") in fetched  # basket and universe names come first
    assert _path("HAVE") not in fetched and _path("T1") not in fetched  # over the cap of two
    assert detail.startswith("tickers=2 ")


async def test_the_catch_up_does_nothing_when_every_name_has_a_close(engine: Engine) -> None:
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as s:
        s.add(AlfaDailyClose(ticker="HAVE", day=date(2026, 10, 9), close=10.0, fetched_at=NOW))
        s.commit()
    feed = _Feed([])
    detail = await hl.hot_daily_jobs(_cfg())[1].run(_ctx(engine, feed, tickers=("HAVE",)))
    assert detail == "nothing missing" and feed.calls == []
