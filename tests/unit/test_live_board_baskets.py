"""Phase 5.26: the "sepete göre" column on the Hisse panosu, and the plumbing behind it."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import baskets
from webapp.board import daily_jobs as dj
from webapp.board.baskets import BasketLag, BasketSettings, basket_close_tickers
from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
from webapp.live_alpha import board as live_board
from webapp.live_alpha import job as live_job

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_board_daily_jobs import _EDT_DUE, _SETTINGS, _Client, _Reader
from tests.unit.test_live_alpha_job import T0, FakeUW, _cycle, _seed
from tests.unit.test_live_alpha_job import app_with_scan as app_with_scan

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

OPTICAL = ("AAOI", "COHR", "LITE", "CIEN", "FN")


def _lag(**over: Any) -> BasketLag:
    base: dict[str, Any] = {
        "basket_id": "optical", "basket_name": "Optik ağ", "state": "measured",
        "start": date(2026, 10, 1), "end": date(2026, 10, 8), "window_sessions": 5,
        "members_priced": 5, "members_total": 5, "residual_pct": -4.2, "peers_pct": 6.0,
        "own_pct": 1.8, "rank": 5, "lagging": True, "leading": False,
    }
    base.update(over)
    return BasketLag(**base)


# ---------------------------------------------------------------------------
# The cell text
# ---------------------------------------------------------------------------


def test_a_ticker_outside_every_basket_says_nothing() -> None:
    assert live_board.basket_text(None) == (None, "gray")


def test_a_lagging_name_states_the_gap_the_window_and_the_coverage() -> None:
    text, tone = live_board.basket_text(_lag())
    assert text is not None
    assert text.startswith("geride: sepete göre %-4.2")
    assert "Optik ağ" in text and "01.10→08.10" in text
    assert "sepette 5. sırada" in text and "5/5 isim fiyatlı" in text
    assert tone == "amber"


def test_a_leading_name_is_flagged_the_same_way() -> None:
    text, tone = live_board.basket_text(_lag(residual_pct=4.2, lagging=False, leading=True, rank=1))
    assert text is not None and text.startswith("önde: sepete göre %+4.2")
    assert tone == "amber"


def test_a_name_inside_the_cutoff_is_gray_and_says_it_moves_with_the_group() -> None:
    text, tone = live_board.basket_text(_lag(residual_pct=0.8, lagging=False, leading=False, rank=3))
    assert text is not None and text.startswith("sepetle birlikte: sepete göre %+0.8")
    assert tone == "gray"


def test_thin_data_names_the_coverage_instead_of_a_number() -> None:
    text, tone = live_board.basket_text(
        _lag(state="thin_data", residual_pct=None, members_priced=2, lagging=False, rank=None),
    )
    assert text == "yetersiz veri · Optik ağ (2/5 isim fiyatlı)"
    assert tone == "gray"


def test_a_flat_basket_says_so_and_shows_no_number() -> None:
    text, tone = live_board.basket_text(_lag(state="flat_basket", residual_pct=None, lagging=False))
    assert text == "sepet hareketsiz · Optik ağ (5/5 isim fiyatlı)"
    assert tone == "gray"


@pytest.mark.parametrize(
    "lag",
    [
        _lag(),
        _lag(residual_pct=4.2, lagging=False, leading=True),
        _lag(residual_pct=0.8, lagging=False, leading=False),
        _lag(state="thin_data", residual_pct=None),
        _lag(state="flat_basket", residual_pct=None),
    ],
)
def test_no_state_uses_an_advice_word(lag: BasketLag) -> None:
    """The column measures. It never says what to do (contract §3.7)."""
    from webapp.board.honesty import forbidden_words

    text, _ = live_board.basket_text(lag)
    assert text is not None
    assert forbidden_words(text) == []


def test_build_rows_without_baskets_keeps_the_row_shape_and_leaves_the_cell_empty() -> None:
    cfg = live_board.load_board_settings()
    rows = live_board.build_rows(["NVDA"], {}, {}, {}, {}, {}, {}, date(2026, 9, 24), cfg)
    assert rows[0]["basket"] is None and rows[0]["basket_tone"] == "gray"


def test_build_rows_carries_the_basket_text_per_ticker() -> None:
    cfg = live_board.load_board_settings()
    rows = live_board.build_rows(
        ["AAOI", "NVDA"], {}, {}, {}, {}, {}, {}, date(2026, 9, 24), cfg, basket={"AAOI": _lag()},
    )
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAOI"]["basket"].startswith("geride")
    assert by_ticker["NVDA"]["basket"] is None


# ---------------------------------------------------------------------------
# Through a real cycle
# ---------------------------------------------------------------------------


def _basket_cfg(members: tuple[str, ...] = ("NVDA", "PEERA", "PEERB", "PEERC")) -> BasketSettings:
    return BasketSettings.model_validate({
        "basket_version": "t", "window_sessions": 5, "min_members_with_data": 3,
        "lag_flag_pct": 3.0, "min_basket_move_pct": 2.0,
        "baskets": [{"id": "t", "name": "Deneme", "members": list(members)}],
    })


def _seed_closes(engine: Engine, final: dict[str, float]) -> None:
    """Six consecutive sessions at 100, then `final` on the newest."""
    from sqlalchemy.orm import Session

    ensure_daily_close_tables(engine)
    start = date(2026, 9, 16)
    stamp = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)
    with Session(engine) as s:
        for ticker, last in final.items():
            for offset in range(5):
                s.add(AlfaDailyClose(ticker=ticker, day=start + timedelta(days=offset), close=100.0, fetched_at=stamp))
            s.add(AlfaDailyClose(ticker=ticker, day=start + timedelta(days=5), close=last, fetched_at=stamp))
        s.commit()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'live.db'}")


def test_a_cycle_puts_the_basket_cell_on_the_board_row(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_closes(engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"].startswith("geride: sepete göre %-10.0")
    assert nvda["basket_tone"] == "amber"


def test_a_basket_failure_costs_the_column_and_not_the_row(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom() -> Any:
        raise ValueError("baskets.yaml is malformed")

    monkeypatch.setattr(live_job, "load_basket_settings", _boom)
    with caplog.at_level("WARNING", logger="webapp.live_alpha.job"):
        row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"] is None
    assert nvda["verdict"] != "—"  # the rest of the row is intact
    assert "basket lags unavailable" in caplog.text


def test_missing_closes_leave_the_cell_honest_not_zero(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_daily_close_tables(engine)  # tables exist, nothing stored yet
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)
    row = _cycle(engine, FakeUW(), T0)
    nvda = {r["ticker"]: r for r in row.snapshot["board"]}["NVDA"]
    assert nvda["basket"] is None  # no anchors at all: nothing to say


@pytest.fixture
def with_baskets(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_closes(engine, {"NVDA": 100.0, "PEERA": 110.0, "PEERB": 110.0, "PEERC": 110.0})
    monkeypatch.setattr(live_job, "load_basket_settings", _basket_cfg)


def test_the_page_shows_the_column_when_a_row_has_a_reading(
    with_baskets: None, app_with_scan: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    body = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch)).get("/").text
    assert "<th class=\"pr-3\">Sepete göre</th>" in body
    row = body.split('data-board-row="NVDA"', 1)[1].split("</tr>", 1)[0]
    assert "data-basket" in row and "geride: sepete göre %-10.0" in row


def test_the_page_omits_the_column_when_no_row_has_a_reading(
    app_with_scan: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    body = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch)).get("/").text
    assert 'data-board-row="NVDA"' in body  # the board is there
    assert "Sepete göre" not in body and "data-basket" not in body


# ---------------------------------------------------------------------------
# The daily-close widening (contract §3.6)
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_engine(tmp_path: Path) -> Engine:
    from webapp.board.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    dj.ensure_job_run_tables(engine)
    return engine


async def test_the_loop_asks_for_basket_closes_and_nothing_else_for_them(tmp_path: Path) -> None:
    """Through the real loop: five more daily closes, and the delayed families unchanged."""
    from tests.unit import test_alfa_refresher_jobs as rj

    url = f"sqlite:///{tmp_path / 'board.db'}"
    rj._seed_today(url)
    client = rj._FakeClient()
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        raise rj._Stop

    from webapp.board.refresher import board_refresh_loop

    with pytest.raises(rj._Stop):
        await board_refresh_loop(
            database_url=url, profile_path=rj._CALIBRATION, board_profile_path=rj._BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: rj._Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: rj._POST_CLOSE, sleep=_sleep,
            close_extra=lambda: OPTICAL,
        )
    # SPY and SMCI as before, plus the five basket members
    assert rj._count(client, "/ohlc/1d") == 2 + len(OPTICAL)
    for ticker in OPTICAL:
        assert any(f"/{ticker}/ohlc/1d" in path for path, _p in client.calls)
    # the four delayed families still see only the board tickers: no request spent on a basket member
    for fragment in ("/recent-trades", "/insider/transactions", "/interest-float/v2", "/ftds"):
        assert rj._count(client, fragment) == 2


async def test_basket_members_reach_only_the_close_job(jobs_engine: Engine) -> None:
    seen: dict[str, tuple[str, ...]] = {}

    async def _run(ctx: dj.JobContext) -> str:
        seen["tickers"], seen["extra"] = ctx.tickers, ctx.close_extra
        return "ok"

    await dj.run_daily_jobs(
        _Client(), jobs_engine, _SETTINGS,
        jobs=[dj.DailyJob(name="probe", et_time=time(7, 15), run=_run)],
        reader=_Reader(), journal=None, now=_EDT_DUE, close_extra=OPTICAL,
    )
    assert seen["extra"] == OPTICAL
    # the delayed families read ctx.tickers and spend four requests per name: no basket member there
    assert not set(OPTICAL) & set(seen["tickers"])


async def test_the_close_job_requests_board_tickers_and_basket_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    async def _stub(client: Any, engine: Any, tickers: Any, *, now: Any) -> Any:
        captured.append(tuple(tickers))
        return type("R", (), {"tickers": (), "degraded": ()})()

    monkeypatch.setattr(dj, "run_daily_close_job", _stub)
    ctx = dj.JobContext(
        client=_Client(), engine=None, sessions=None, settings=_SETTINGS,  # type: ignore[arg-type]
        now=_EDT_DUE, tickers=("NVDA",), reader=_Reader(), close_extra=("AAOI", "COHR"),
    )
    await dj.run_daily_closes(ctx)
    assert captured == [("NVDA", "AAOI", "COHR")]


async def test_without_baskets_the_close_job_asks_for_what_it_always_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    async def _stub(client: Any, engine: Any, tickers: Any, *, now: Any) -> Any:
        captured.append(tuple(tickers))
        return type("R", (), {"tickers": (), "degraded": ()})()

    monkeypatch.setattr(dj, "run_daily_close_job", _stub)
    ctx = dj.JobContext(
        client=_Client(), engine=None, sessions=None, settings=_SETTINGS,  # type: ignore[arg-type]
        now=_EDT_DUE, tickers=("NVDA",), reader=_Reader(),
    )
    await dj.run_daily_closes(ctx)
    assert captured == [("NVDA",)]


def test_basket_close_tickers_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(path: Any = None) -> Any:
        raise OSError("no such file")

    monkeypatch.setattr(baskets, "load_basket_settings", _boom)
    assert basket_close_tickers() == ()


def test_basket_close_tickers_reads_the_committed_profile() -> None:
    assert set(OPTICAL) <= set(basket_close_tickers())


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_a_malformed_basket_profile_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    import webapp.main as m

    def _boom() -> Any:
        raise ValueError("baskets_v1.yaml: members must not repeat")

    monkeypatch.setattr(m, "load_basket_settings", _boom)
    with pytest.raises(ValueError, match="members must not repeat"):
        m._load_board_profiles()
