"""Phase 5.2.A1: ``GET /alfa`` renders the per-ticker board from the database only.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 (render budget, no UW
calls at render time), §5 A1, §10 (route test on a seeded sqlite run,
render-budget test, zero-UW-calls test); decision P12.

Signals are seeded through ``SqliteBacktestStore`` into a tmp sqlite file, and
print meta straight into ``alfa_print_meta``.

Pins:
  - one ``<article data-row>`` per (ticker, side-aware direction), ordered by
    total premium; a sold call renders under ``aşağı``; legacy and mid-side
    prints carry the fallback marker; every print is listed in the detail;
  - the run selector defaults to the newest run and honours ``?run=``;
  - no runs, and a failed database read, render their own states; a failed
    read never looks like an empty board;
  - the combined score does not appear on the page;
  - zero Unusual Whales calls during ``GET /alfa``;
  - render budget: p95 <= 1.5 s over 20 renders of a 2,000-signal run.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from webapp.board.alfa_page import ALFA_COPY
from webapp.board.direction import FALLBACK_MARKER
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from fastapi.testclient import TestClient

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_LIVE = "live-2026-09-15"
_ROW = re.compile(r'data-row data-ticker="([^"]+)" data-direction="([^"]+)"')
_RENDER_BUDGET_P95_S = 1.5  # contract §4.1


@dataclass(frozen=True)
class _Spec:
    event_id: str
    ticker: str
    option_type: str
    strike: str
    premium: str
    fill_side: str | None  # None: a legacy row without print meta
    dte: int = 3
    second: int = 0
    score: float = 0.3
    chain: str | None = None


def _seed(url: str, run_id: str, specs: Sequence[_Spec], *, ts: datetime = _TS) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(specs) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        for s in specs:
            pr = build_print(
                event_id=s.event_id, ts=ts + timedelta(seconds=s.second), ticker=s.ticker,
                option_type=s.option_type,  # type: ignore[arg-type]
                strike=s.strike, dte=s.dte, premium=s.premium,
            )
            store.add(
                EnrichedEvent(print=pr, combined_score_post_penalty=s.score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()  # flushes the buffer and finishes the run
    with_meta = [s for s in specs if s.fill_side is not None]
    if not with_meta:
        return
    engine = create_engine(url)
    try:
        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            session.add_all(
                AlfaPrintMeta(
                    run_id=run_id, event_id=s.event_id, ticker=s.ticker,
                    option_chain=s.chain, fill_side=s.fill_side, option_type=s.option_type,
                    strike=s.strike, expiry=(ts + timedelta(days=s.dte)).date(),
                    print_ts=ts, written_at=ts,
                )
                for s in with_meta
            )
            session.commit()
    finally:
        engine.dispose()


def _reset_singletons(m: Any) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, TestClient]]:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    import webapp.main as m

    _reset_singletons(m)
    yield url, authed_client(m.app, monkeypatch)
    _reset_singletons(m)


def _row_html(body: str, ticker: str, direction: str) -> str:
    marker = f'data-ticker="{ticker}" data-direction="{direction}"'
    for chunk in body.split("<article")[1:]:
        if marker in chunk:
            return chunk.split("</article>")[0]
    raise AssertionError(f"no row {ticker}/{direction}")


_SPECS = [
    _Spec("s1", "SPY", "call", "760", "400000", "at_ask", chain="SPY260918C00760000"),
    _Spec("s2", "SPY", "call", "760", "100000", "at_ask", second=10),
    _Spec("s3", "SPY", "call", "770", "50000", "at_bid", second=20),  # sold call
    _Spec("s4", "NVDA", "put", "170", "80000", None, second=30),  # legacy row
    _Spec("s5", "NVDA", "put", "165", "20000", "midpoint", second=40),
]


def test_alfa_renders_one_row_per_ticker_and_side_aware_direction(
    board: tuple[str, TestClient],
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS)

    response = client.get("/alfa")
    assert response.status_code == 200
    body = response.text

    assert 'href="/"' in body  # Phase 5.2.A7 (D10): the nav links the board at "/"; /alfa is an alias
    assert _ROW.findall(body) == [("SPY", "up"), ("NVDA", "down"), ("SPY", "down")]
    assert "3 satır · 5 baskı" in body

    spy_up = _row_html(body, "SPY", "up")
    assert "yukarı" in spy_up
    assert "$500,000" in spy_up
    assert "data-print-count>2<" in spy_up
    assert "call 760 · 2026-09-18" in spy_up
    assert "SPY260918C00760000" in spy_up
    assert "data-concentration>%100<" in spy_up
    assert "data-dominance>%91<" in spy_up
    assert 'data-position="intentional">kasıtlı pozisyon<' in spy_up
    assert "2 baskı alım/satım tarafından · 0 baskı opsiyon tipinden" in spy_up
    assert FALLBACK_MARKER not in spy_up
    assert spy_up.count("data-print=") == 2

    nvda_down = _row_html(body, "NVDA", "down")
    assert "aşağı" in nvda_down
    assert FALLBACK_MARKER in nvda_down
    assert "0 baskı alım/satım tarafından · 2 baskı opsiyon tipinden" in nvda_down
    assert nvda_down.count("data-print=") == 2

    spy_down = _row_html(body, "SPY", "down")
    assert "aşağı" in spy_down
    assert "data-dominance>%9<" in spy_down
    assert "bid (satım)" in spy_down


def test_alfa_run_selector_defaults_to_newest_and_honours_run(
    board: tuple[str, TestClient],
) -> None:
    url, client = board
    _seed(url, "backtest-x", [_Spec("b1", "AAPL", "call", "230", "90000", "at_ask")],
          ts=datetime(2026, 7, 1, 14, 0, tzinfo=UTC))
    _seed(url, _LIVE, [_Spec("l1", "SPY", "put", "740", "70000", "at_ask")])

    assert _ROW.findall(client.get("/alfa").text) == [("SPY", "down")]
    chosen = client.get("/alfa", params={"run": "backtest-x"}).text
    assert _ROW.findall(chosen) == [("AAPL", "up")]
    assert '<option value="backtest-x" selected>' in chosen
    assert _ROW.findall(client.get("/alfa", params={"run": "no-such-run"}).text) == [("SPY", "down")]


def test_alfa_without_runs_shows_the_no_runs_state(board: tuple[str, TestClient]) -> None:
    url, client = board
    SqliteBacktestStore(url).close()  # schema exists, no run stored yet
    body = client.get("/alfa").text
    assert 'data-state="no-runs"' in body
    assert ALFA_COPY["no_runs"] in body
    assert 'data-state="load-failed"' not in body
    assert "data-row" not in body


def test_alfa_on_an_uninitialised_database_says_it_could_not_read(
    board: tuple[str, TestClient],
) -> None:
    _url, client = board  # no signal table at all: the read fails, it is not "no runs"
    body = client.get("/alfa").text
    assert 'data-state="load-failed"' in body
    assert 'data-state="no-runs"' not in body


def test_alfa_read_failure_is_never_an_empty_board(
    board: tuple[str, TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS[:1])
    import webapp.main as m

    class _BrokenReader:
        def load_run(self, run_id: str) -> list[object]:
            msg = "database unavailable"
            raise RuntimeError(msg)

    monkeypatch.setattr(m, "_board_reader", lambda: _BrokenReader())
    body = client.get("/alfa").text
    assert 'data-state="load-failed"' in body
    assert ALFA_COPY["load_failed"] in body
    assert 'data-state="empty-run"' not in body

    class _BrokenRepo:
        def runs(self) -> list[object]:
            msg = "database unavailable"
            raise RuntimeError(msg)

    monkeypatch.setattr(m, "_repo", lambda: _BrokenRepo())
    body = client.get("/alfa").text
    assert 'data-state="load-failed"' in body
    assert 'data-state="no-runs"' not in body


_AUDIT_BLOCK = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)


def test_alfa_shows_the_combined_score_only_in_the_audit_block(board: tuple[str, TestClient]) -> None:
    """Phase 5.2.A4 (R-EV2) moved the score into the row's Denetim block; it stays off the face."""
    url, client = board
    _seed(url, _LIVE, [_Spec("x1", "SPY", "call", "760", "123000", "at_ask", score=0.7777)])
    body = client.get("/alfa").text
    assert "data-row" in body
    assert "0.7777" not in body
    assert "0.78" not in _AUDIT_BLOCK.sub("", body)
    (audit,) = _AUDIT_BLOCK.findall(body)
    assert "Birleşik skor (denetim, sınırsız ölçek): 0.78" in audit


def test_alfa_makes_zero_unusual_whales_calls(
    board: tuple[str, TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS)
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"GET /alfa must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    for _ in range(2):
        response = client.get("/alfa")
        assert response.status_code == 200
        assert "data-row" in response.text
    assert calls == []


def _budget_specs(n: int) -> list[_Spec]:
    tickers = [f"T{i:02d}" for i in range(25)]
    sides: list[str | None] = ["at_ask", "at_bid", "midpoint", None]
    return [
        _Spec(
            event_id=f"b{i:05d}",
            ticker=tickers[i % len(tickers)],
            option_type="call" if i % 3 else "put",
            strike=str(100 + (i % 10) * 5),
            premium=str(25000 + i * 7),
            fill_side=sides[i % len(sides)],
            dte=3 + (i % 4) * 7,
            second=i,
        )
        for i in range(n)
    ]


def test_alfa_render_budget_p95_at_2000_signals(board: tuple[str, TestClient]) -> None:
    url, client = board
    _seed(url, _LIVE, _budget_specs(2000))

    durations: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get("/alfa")
        durations.append(time.perf_counter() - start)
        assert response.status_code == 200
    body = response.text
    assert "2000 baskı" in body
    assert body.count("data-print=") == 2000

    durations.sort()
    p95 = durations[math.ceil(0.95 * len(durations)) - 1]
    assert p95 <= _RENDER_BUDGET_P95_S, f"p95 {p95:.3f}s over budget; all: {durations}"
