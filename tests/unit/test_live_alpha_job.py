"""Phase 5.25 Live Alpha: one real cycle end to end on SQLite with a fake UW client.

The trace the contract asks for (§11, master prompt §18): API record and headline →
thesis → decision → stock plan → option pricing → PAPER event — and the negative
paths: quota refusal, record immutability, one PAPER per opportunity, restart
survival, unauthenticated access, cross-origin actions, a stale snapshot never
staying a green ALIM.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session
from webapp.board import quota_ledger
from webapp.board.atm import AlfaAtm, AlfaAtmExpiry, ensure_atm_tables
from webapp.board.daily_close import AlfaDailyBar, ensure_daily_bar_tables
from webapp.board.db import make_engine
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables
from webapp.live_alpha import store
from webapp.live_alpha.job import load_costs, run_cycle

from tests.conftest import build_print
from tests.unit._webapp_auth import TEST_PASSWORD, TEST_USER, basic_auth_header, set_web_auth
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.live_alpha.settings import load_live_alpha_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

RUN = "live-2026-09-24"
T0 = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)          # Thu 11:00 ET, session live
EXPIRY = date(2026, 10, 16)
SETTINGS = load_live_alpha_settings()
BOARD = load_board_settings()
COSTS = load_costs()


class FakeUW:
    """Answers the two Live Alpha endpoints; counts calls; can be told to fail news."""

    def __init__(self, *, news_error: Exception | None = None, headline_age_hours: float = 2.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.news_error = news_error
        self.headline_age_hours = headline_age_hours
        self.now = T0
        self.last_daily_request_count: int | None = None

    async def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        if path == "/api/news/headlines":
            if self.news_error is not None:
                raise self.news_error
            created = (self.now - timedelta(hours=self.headline_age_hours)).isoformat().replace("+00:00", "Z")
            return {"data": [
                {"created_at": created, "headline": "NVDA wins a data-center contract", "is_major": True,
                 "meta": {}, "sentiment": "positive", "source": "BusinessWire", "tags": ["contract"],
                 "tickers": ["NVDA"]},
                {"created_at": created, "headline": "NVDA wins a data-center contract", "is_major": True,
                 "meta": {}, "sentiment": "positive", "source": "Benzinga", "tags": [], "tickers": ["NVDA"]},
                {"created_at": created, "headline": "Unrelated AMD story", "is_major": False, "meta": {},
                 "sentiment": "neutral", "source": "X", "tags": [], "tickers": ["AMD"]},
            ]}
        if path.endswith("/option-contracts"):
            symbols = (params or {}).get("option_symbol[]", [])
            rows = []
            for sym in symbols:
                strike = int(sym[-8:]) / 1000
                if strike % 5:          # only $5 strikes are "listed"
                    continue
                ask = max(0.2, 4.0 - 0.4 * (strike - 100))
                rows.append({"option_symbol": sym, "nbbo_bid": f"{ask / 2:.2f}", "nbbo_ask": f"{ask:.2f}",
                             "last_price": f"{ask:.2f}", "volume": 100, "open_interest": 1000,
                             "last_tape_time": self.now.isoformat()})
            return {"data": rows}
        raise AssertionError(f"unexpected path {path}")


def _seed(url: str) -> Engine:
    st = SqliteBacktestStore(url, flush_threshold=10)
    try:
        st.start_run(profile=load_default_profile(), universe_id="live", run_id=RUN)
        for i, strike in enumerate(("100", "105", "110")):
            pr = build_print(event_id=f"n{i}", ts=T0 - timedelta(minutes=30 - i * 5), ticker="NVDA",
                             option_type="call", strike=strike, dte=22, premium="120000")
            st.add(EnrichedEvent(print=pr), LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t"),
                   PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5))
    finally:
        st.close()
    engine = make_engine(url)
    ensure_telemetry_tables(engine)
    ensure_atm_tables(engine)
    ensure_daily_bar_tables(engine)
    store.ensure_live_tables(engine)
    quota_ledger.ensure_quota_tables(engine)
    with Session(engine) as s:
        for i, strike in enumerate(("100", "105", "110")):
            s.add(AlfaPrintMeta(run_id=RUN, event_id=f"n{i}", ticker="NVDA",
                                option_chain=f"NVDA261016C00{int(strike):03d}000", fill_side="at_ask",
                                option_type="call", strike=strike, expiry=EXPIRY, print_ts=T0, written_at=T0))
        for ticker, close in (("NVDA", 99.0), ("SPY", 500.0)):
            day = date(2026, 8, 10)
            n = 0
            while n < 30:
                if day.weekday() < 5:
                    s.add(AlfaDailyBar(ticker=ticker, day=day, open=close, high=close + 1, low=close - 1,
                                       close=close, fetched_at=T0))
                    n += 1
                day += timedelta(days=1)
                if day >= date(2026, 9, 24):
                    break
        s.add(AlfaAtm(ticker="NVDA", expiry=EXPIRY, strike=100.0, stock_price=100.0, fetched_at=T0 - timedelta(seconds=60),
                      trade_date=date(2026, 9, 24)))
        s.add(AlfaAtm(ticker="SPY", expiry=EXPIRY, strike=500.0, stock_price=500.0, fetched_at=T0 - timedelta(seconds=60),
                      trade_date=date(2026, 9, 24)))
        s.add(AlfaAtmExpiry(ticker="NVDA", expiry=date(2026, 9, 25), fetched_at=T0))
        s.add(AlfaAtmExpiry(ticker="NVDA", expiry=EXPIRY, fetched_at=T0))
        s.commit()
    return engine


def _move_spot(engine: Engine, price: float, at: datetime) -> None:
    with engine.begin() as c:
        c.execute(update(AlfaAtm).where(AlfaAtm.ticker == "NVDA").values(stock_price=price, fetched_at=at))


def _cycle(engine: Engine, client: FakeUW, now: datetime, board: Any = BOARD) -> store.ScanRow:
    client.now = now
    reader = BoardSignalReader(engine=engine)
    return asyncio.run(run_cycle(engine, reader, client, SETTINGS, board, COSTS, now))


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'live.db'}")


def test_cycle_produces_a_sourced_buy_with_a_plan_and_a_priced_option(engine: Engine) -> None:
    client = FakeUW()
    row = _cycle(engine, client, T0)
    assert row.market_mode == "LIVE" and row.status == "ok"
    cards = row.snapshot["cards"]
    nvda = next(c for c in cards if c["ticker"] == "NVDA")
    assert nvda["recommendation"] == "BUY" and nvda["readiness"] == "READY"
    assert nvda["path"] == "P1_news_continuation"
    # the plan: spot 100, prev close 99, ATR 2 -> limit 101, stop 97, target 106
    assert nvda["stock_plan"]["chase_limit"] == 101.0 and nvda["stock_plan"]["stop"] == 97.0
    # the syndicated duplicate is one story, and the AMD headline is not NVDA news
    assert nvda["news_evidence"].count("NVDA wins") == 1 and "AMD" not in nvda["news_evidence"]
    # options were priced from returned (= listed) contracts; wide markets make the stock preferred
    kinds = {o["kind"] for o in nvda["options"]}
    assert kinds == {"long_call", "bull_call_debit"}
    assert all(leg["option_symbol"].startswith("NVDA261016C") for o in nvda["options"] for leg in o["legs"])
    assert nvda["instrument"]["preferred"] == "stock"
    # one rec, one PAPER pending, requests reserved through the shared ledger
    assert store.latest_recs(engine, [nvda["opportunity_id"]])
    assert [p.state for p in store.papers(engine)] == ["PAPER_PENDING"]
    assert row.snapshot["requests"]["reserved"] == len(client.calls) == 2
    assert quota_ledger.reserved_on(engine, date(2026, 9, 24)) == 2
    assert row.snapshot["funnel"]["giriş_hazır"] == 1


def test_paper_fills_only_on_a_later_price_then_exits_at_the_stop(engine: Engine) -> None:
    client = FakeUW()
    _cycle(engine, client, T0)
    assert store.papers(engine)[0].state == "PAPER_PENDING"      # same price as the decision: no fill
    _move_spot(engine, 100.5, T0 + timedelta(minutes=4))
    second = _cycle(engine, client, T0 + timedelta(minutes=5))
    paper = store.papers(engine)[0]
    assert paper.state == "PAPER_OPEN"
    assert paper.entry_price == pytest.approx(100.5 * 1.0005)
    assert paper.quantity == 25
    # unchanged decision -> no new record
    opp = second.snapshot["cards"][0]["opportunity_id"]
    assert len(store.recs_for(engine, opp)) == 1
    _move_spot(engine, 96.9, T0 + timedelta(minutes=9))
    third = _cycle(engine, client, T0 + timedelta(minutes=10))
    paper = store.papers(engine)[0]
    assert paper.state == "CLOSED_SIMULATED" and paper.exit_reason == "stop"
    assert paper.pnl_usd == round((round(96.9 * 0.9995, 4) - round(100.5 * 1.0005, 4)) * 25, 2)
    # the decision changed (price now against the flow): a NEW record that supersedes the old one
    recs = store.recs_for(engine, opp)
    assert [r["recommendation"] for r in recs] == ["BUY", "AVOID"]
    assert recs[1]["supersedes"] == recs[0]["rec_id"]
    assert recs[0]["card"]["recommendation"] == "BUY"     # the first record was not rewritten
    kinds = [e["kind"] for e in store.events_for(engine, opp)]
    assert kinds.count("paper_fill") == 1 and "paper_exit" in kinds
    assert third.snapshot["paper"]["closed"] == 1


def test_one_opportunity_gets_one_paper_position(engine: Engine) -> None:
    client = FakeUW()
    _cycle(engine, client, T0)
    _cycle(engine, client, T0 + timedelta(seconds=30))
    assert len(store.papers(engine)) == 1


def test_news_is_not_refetched_inside_the_refresh_window(engine: Engine) -> None:
    client = FakeUW()
    _cycle(engine, client, T0)
    _cycle(engine, client, T0 + timedelta(minutes=5))
    assert sum(1 for p, _ in client.calls if p == "/api/news/headlines") == 1


def test_quota_refusal_is_a_failure_state_not_no_news(engine: Engine) -> None:
    board = BOARD.model_copy(update={"refresh": BOARD.refresh.model_copy(update={"daily_request_soft_cap": 1})})
    quota_ledger.reserve(engine, day=date(2026, 9, 24), n=1, cap=1)
    client = FakeUW()
    row = _cycle(engine, client, T0, board)
    assert client.calls == []
    assert row.snapshot["requests"]["refused"] >= 1
    nvda = row.snapshot["cards"][0]
    assert "kontrol edilemedi" in nvda["news_evidence"]
    assert "başlık yok" not in nvda["news_evidence"]
    assert row.status == "degraded"


def test_provider_error_is_never_supportive(engine: Engine) -> None:
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesTransientError

    client = FakeUW(news_error=UnusualWhalesTransientError("boom"))
    row = _cycle(engine, client, T0)
    nvda = row.snapshot["cards"][0]
    # price +1 % vs SPY 0 % still qualifies P2 on its own; the failed news read is stated, not counted
    assert nvda["path"] == "P2_market_relative_flow"
    assert "kontrol edilemedi" in nvda["news_evidence"]


def test_closed_market_cycle_has_no_ready_entry(engine: Engine) -> None:
    closed = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)
    row = _cycle(engine, FakeUW(), closed)
    assert row.market_mode == "CLOSED"
    assert all(c["readiness"] != "READY" for c in row.snapshot["cards"])
    assert store.papers(engine) == []
    # outside the session the NBBO belongs to the last session: never an executable option entry
    for card in row.snapshot["cards"]:
        for o in card["options"]:
            assert o["readiness"] in ("QUOTE_PENDING", "INVALID")


def test_snapshot_survives_a_restart(engine: Engine, tmp_path: Path) -> None:
    row = _cycle(engine, FakeUW(), T0)
    engine.dispose()
    fresh = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    got = store.latest_scan(fresh)
    assert got is not None and got.scan_id == row.scan_id
    assert got.snapshot["cards"][0]["recommendation"] == "BUY"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_scan(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import webapp.live_alpha.page as page
    import webapp.main as m

    _cycle(engine, FakeUW(), T0)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'live.db'}")
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None
    page._ready_engines.clear()
    monkeypatch.setattr(m, "_now", lambda: T0 + timedelta(minutes=1))
    return m


def test_root_requires_auth(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    set_web_auth(monkeypatch)
    client = TestClient(app_with_scan.app)
    assert client.get("/").status_code == 401
    wrong = basic_auth_header(TEST_USER, TEST_PASSWORD + "x")
    assert client.get("/", headers=wrong).status_code == 401


def test_root_renders_the_card(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch))
    body = client.get("/").text
    assert 'id="live-alpha"' in body and "Bugünün Fırsatları" in body
    assert 'data-card="NVDA" data-rec="BUY" data-ready="READY"' in body
    assert "ALIM" in body and "Tezi bozan durum" in body and "En önemli karşı argüman" in body
    assert "Şimdi alınabilir: NVDA." in body
    # plain Turkish first: action with prices, then the reason in everyday words
    assert "AL — $100.00 civarından, en fazla $101.00&#39;e kadar." in body
    assert "Zarar-kes $97.00, hedef $106.00" in body
    assert "yükselişe oynuyor" in body and "NVDA wins a data-center contract" in body
    assert "data-withdrawn" not in body


def test_stale_snapshot_withdraws_the_green_buy(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_with_scan, "_now", lambda: T0 + timedelta(hours=2))
    client = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch))
    body = client.get("/").text
    assert "data-stale" in body and "data-withdrawn" in body
    assert "Şimdi alınabilir" not in body


def test_closed_market_view_withdraws_ready_entries(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_with_scan, "_now", lambda: datetime(2026, 9, 24, 20, 5, tzinfo=UTC))
    client = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch))
    assert "data-withdrawn" in client.get("/").text


def test_actions_need_same_origin_and_write_events(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch))
    engine = app_with_scan._board_reader().engine
    opp = store.latest_scan(engine).snapshot["cards"][0]["opportunity_id"]  # type: ignore[union-attr]
    form = {"opportunity_id": opp, "rec_id": ""}
    assert client.post("/canli/izle", data=form, follow_redirects=False).status_code == 403
    same = {"origin": "http://testserver"}
    assert client.post("/canli/izle", data=form, headers=same, follow_redirects=False).status_code == 303
    foreign = {"origin": "http://evil.example"}
    assert client.post("/canli/pas", data=form, headers=foreign, follow_redirects=False).status_code == 403
    manual = {**form, "instrument": "stock", "side": "buy", "quantity": "10", "price": "100.2", "note": "test"}
    assert client.post("/canli/manuel", data=manual, headers=same, follow_redirects=False).status_code == 303
    # PAPER already exists for this BUY (the job opened it): the owner button does not open a second
    r = client.post("/canli/paper", data=form, headers=same, follow_redirects=False)
    assert r.status_code == 303 and "paper_var" in r.headers["location"]
    kinds = [e["kind"] for e in store.events_for(engine, opp)]
    assert "owner_izle" in kinds and "manual_fill" in kinds and "owner_pas" not in kinds
    assert [m.ticker for m in store.manuals(engine)] == ["NVDA"]
    history = client.get(f"/canli/{opp}")
    assert history.status_code == 200 and "BUY / READY" in history.text


def test_unknown_opportunity_writes_nothing(app_with_scan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch))
    r = client.post("/canli/izle", data={"opportunity_id": "nope"}, headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303 and "ok=yok" in r.headers["location"]
    engine = app_with_scan._board_reader().engine
    assert store.events_for(engine, "nope") == []


def test_snapshot_json_round_trips(engine: Engine) -> None:
    row = _cycle(engine, FakeUW(), T0)
    assert json.loads(store.dumps(row.snapshot))["cards"][0]["ticker"] == "NVDA"


@pytest.mark.parametrize(
    ("error", "state", "fragment"),
    [
        ("auth", "failed", "yetki"),
        ("notfound", "failed", "404"),
        ("daily", "failed", "günlük istek limiti"),
        ("shape", "failed", "beklenmeyen yanıt"),
    ],
)
def test_news_error_states(engine: Engine, error: str, state: str, fragment: str) -> None:
    from webapp.live_alpha import uw

    from uoa_detector.sources.unusual_whales.client import (
        UnusualWhalesAuthError,
        UnusualWhalesDailyLimitError,
        UnusualWhalesNotFoundError,
    )

    class _Client:
        last_daily_request_count = None

        async def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
            if error == "auth":
                raise UnusualWhalesAuthError("401")
            if error == "notfound":
                raise UnusualWhalesNotFoundError("404", status_code=404)
            if error == "daily":
                raise UnusualWhalesDailyLimitError("limit")
            return {"rows": []}

    budget = uw.Budget(engine=engine, day=date(2026, 9, 24), cap=100, client=_Client())
    check = asyncio.run(uw.news_for(engine, _Client(), budget, "NVDA", T0, refresh_seconds=900, limit=20))
    assert check.state.value == state and fragment in check.detail
    assert check.items == ()
    assert budget.reserved == 1      # the reservation is spent even when the call failed


# ---------------------------------------------------------------------------
# Review 2026-09-24 regressions
# ---------------------------------------------------------------------------


def test_yesterdays_flow_never_makes_an_entry_ready(engine: Engine) -> None:
    next_day = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    _move_spot(engine, 100.0, next_day - timedelta(seconds=60))
    row = _cycle(engine, FakeUW(), next_day)
    assert row.snapshot["flow_current"] is False
    nvda = row.snapshot["cards"][0]
    assert nvda["readiness"] != "READY" and nvda["recommendation"] != "BUY"
    assert any("bugünkü seansa ait değil" in b for b in nvda["blockers"])
    assert store.papers(engine) == []


def test_stale_spy_is_not_used_for_relative_strength(engine: Engine) -> None:
    from webapp.live_alpha import inputs

    with engine.begin() as c:
        c.execute(update(AlfaAtm).where(AlfaAtm.ticker == "SPY").values(fetched_at=T0 - timedelta(hours=2)))
    prices = inputs.price_contexts(engine, ["NVDA"], "SPY", atr_period=14, atr_min_sessions=20,
                                   max_age_seconds=900)
    assert prices["NVDA"].benchmark_move is None and prices["NVDA"].relative is None


def test_headline_without_ticker_tags_is_not_the_tickers_news() -> None:
    from webapp.live_alpha.uw import parse_headlines

    payload = {"data": [{"created_at": "2026-09-24T13:00:00Z", "headline": "Markets rally", "source": "X",
                         "tickers": []}]}
    assert parse_headlines(payload, "NVDA", T0) == []


def test_pending_paper_does_not_fill_below_the_stop(engine: Engine) -> None:
    client = FakeUW()
    _cycle(engine, client, T0)
    _move_spot(engine, 96.5, T0 + timedelta(minutes=4))     # below the 97 stop
    _cycle(engine, client, T0 + timedelta(minutes=5))
    assert store.papers(engine)[0].state == "PAPER_PENDING"


def test_open_stock_paper_exits_from_daily_bars_when_spot_is_stale(engine: Engine) -> None:
    client = FakeUW()
    _cycle(engine, client, T0)
    _move_spot(engine, 100.5, T0 + timedelta(minutes=4))
    _cycle(engine, client, T0 + timedelta(minutes=5))
    assert store.papers(engine)[0].state == "PAPER_OPEN"
    # Fri 25 Sep: one bar that crossed BOTH the 97 stop and the 106 target -> the stop wins,
    # and it gapped open below the stop -> exit at the open (96.0), not at 97.
    with Session(engine) as s:
        s.add(AlfaDailyBar(ticker="NVDA", day=date(2026, 9, 25), open=96.0, high=107.0, low=95.0, close=100.0,
                           fetched_at=T0))
        s.commit()
    monday = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)          # the spot is still the stale Thursday value
    _cycle(engine, client, monday)
    paper = store.papers(engine)[0]
    assert paper.state == "CLOSED_SIMULATED" and paper.exit_reason.startswith("stop (günlük bar")
    assert paper.exit_price == round(96.0 * 0.9995, 4)


def test_exit_signal_is_sticky_then_unresolved_without_a_price(engine: Engine) -> None:
    from webapp.live_alpha import job

    paper = store.LivePaper(
        paper_id="p1", opportunity_id="2026-09-24:NVDA:up", rec_id="r1", ticker="NVDA", direction="up",
        instrument="long_call",
        plan_json=store.dumps({"instrument": "long_call", "fill_session": "2026-09-24",
                               "stock_plan": {"stop": 97.0, "target": 106.0, "chase_limit": 101.0, "shares": 25},
                               "structure": {"kind": "long_call", "multiplier": 100, "commission_usd": 1.3,
                                             "legs": [{"option_symbol": "NVDA261016C00100000", "right": "call",
                                                       "strike": 100.0, "expiry": "2026-10-16", "is_long": True}]}}),
        state="PAPER_OPEN", created_at=T0, entry_at=T0, entry_price=2.0, quantity=1,
    )
    store.create_paper(engine, paper)

    class _NoQuotes(FakeUW):
        async def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
            if path.endswith("/option-contracts"):
                return {"data": []}
            return await super().request_json(path, params=params, method=method)

    client = _NoQuotes()
    _move_spot(engine, 96.0, T0 + timedelta(minutes=4))       # stop crossed, no option quote
    _cycle(engine, client, T0 + timedelta(minutes=5))
    got = next(p for p in store.papers(engine) if p.paper_id == "p1")
    assert got.state == "EXIT_SIGNALLED" and got.exit_reason == "stop"
    _move_spot(engine, 100.0, T0 + timedelta(minutes=9))      # the bounce must not revive it
    _cycle(engine, client, T0 + timedelta(minutes=10))
    got = next(p for p in store.papers(engine) if p.paper_id == "p1")
    assert got.state == "EXIT_SIGNALLED" and got.exit_reason == "stop"
    friday = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    _cycle(engine, client, friday)
    got = next(p for p in store.papers(engine) if p.paper_id == "p1")
    assert got.state == "UNRESOLVED" and got.pnl_usd is None and got.exit_price is None
    assert job.PAPER_ACTIVE and "UNRESOLVED" not in job.PAPER_ACTIVE


def test_health_shows_the_last_cycle_once_the_job_has_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from webapp.live_alpha import job
    from webapp.main import health

    monkeypatch.setattr(job, "LAST_CYCLE", {"at": "2026-09-24T15:00:00+00:00", "status": "ok", "mode": "LIVE", "cards": 3})
    body = health()
    assert body["live_alpha"] == {"at": "2026-09-24T15:00:00+00:00", "status": "ok", "mode": "LIVE", "cards": 3}


def test_lifespan_starts_the_live_alpha_thread_with_the_live_database(monkeypatch: pytest.MonkeyPatch) -> None:
    import webapp.main as m
    from fastapi.testclient import TestClient

    from tests.unit.test_alfa_refresher_lifespan import _forever, _live_config

    started: list[str] = []
    monkeypatch.setattr(m, "live_config_from_env", lambda: _live_config("sqlite:///live-thread.db"))
    monkeypatch.setattr(m, "run_live_worker", _forever)
    monkeypatch.setattr(m, "gamma_refresh_loop", _forever)
    monkeypatch.setattr(m, "board_refresh_loop", _forever)
    monkeypatch.setattr(m, "_start_live_alpha_thread", started.append)
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200
    assert started == ["sqlite:///live-thread.db"]


def test_premarket_view_names_todays_session_not_tomorrow(
    app_with_scan: Any, monkeypatch: pytest.MonkeyPatch, engine: Engine,
) -> None:
    from fastapi.testclient import TestClient

    premarket = datetime(2026, 9, 24, 11, 0, tzinfo=UTC)
    _cycle(engine, FakeUW(), premarket)
    monkeypatch.setattr(app_with_scan, "_now", lambda: premarket + timedelta(minutes=1))
    body = TestClient(app_with_scan.app, headers=set_web_auth(monkeypatch)).get("/").text
    assert "Bugünkü seans 2026-09-24 09:30 ET'de açılır" in body
    assert "Sonraki seans" not in body
