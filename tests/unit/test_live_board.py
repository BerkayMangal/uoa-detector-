"""Phase 5.25.9: the "Hisse panosu" (display only), worked by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.atm import AlfaAtm
from webapp.live_alpha import board as live_board

from tests.unit.test_live_alpha_job import EXPIRY, RUN, T0, FakeUW, _cycle, _seed
from uoa_detector.live_alpha.model import CheckState, PriceContext

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

CFG = live_board.load_board_settings()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return _seed(f"sqlite:///{tmp_path / 'board.db'}")


def test_expected_move_is_the_atm_straddle_over_spot(engine: Engine) -> None:
    with Session(engine) as s:
        row = s.get(AlfaAtm, ("NVDA", EXPIRY))
        assert row is not None
        row.call_bid, row.call_ask, row.put_bid, row.put_ask = 2.9, 3.1, 1.9, 2.1
        s.commit()
    # straddle mid = 3.0 + 2.0 = 5.0; spot 100 -> ±5.0 %
    moves = live_board.expected_moves(engine, ["NVDA"], date(2026, 9, 24), CFG)
    assert moves["NVDA"].pct == pytest.approx(0.05)
    assert moves["NVDA"].expiry == EXPIRY


def test_expected_move_skips_crossed_or_missing_quotes(engine: Engine) -> None:
    with Session(engine) as s:
        row = s.get(AlfaAtm, ("NVDA", EXPIRY))
        assert row is not None
        row.call_bid, row.call_ask, row.put_bid, row.put_ask = 3.2, 3.1, 1.9, 2.1
        s.commit()
    assert "NVDA" not in live_board.expected_moves(engine, ["NVDA"], date(2026, 9, 24), CFG)


def test_flow_pace_compares_the_same_time_of_day(engine: Engine) -> None:
    from tests.conftest import build_print
    from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
    from uoa_detector.calibration import load_default_profile
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.domain.labels import LabelDecision, SignalLabel
    from uoa_detector.domain.risk import PositionSize, RiskBucket

    url = str(engine.url)
    # three prior sessions, each with 1 print at 10:00 ET and 5 prints in the afternoon
    for day in (21, 22, 23):
        st = SqliteBacktestStore(url, flush_threshold=10)
        try:
            run = f"live-2026-09-{day}"
            st.start_run(profile=load_default_profile(), universe_id="live", run_id=run)
            morning = datetime(2026, 9, day, 14, 0, tzinfo=UTC)
            afternoon = datetime(2026, 9, day, 18, 0, tzinfo=UTC)
            for i, ts in enumerate([morning] + [afternoon + timedelta(minutes=k) for k in range(5)]):
                st.add(EnrichedEvent(print=build_print(event_id=f"{day}-{i}", ts=ts, ticker="NVDA")),
                       LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t"),
                       PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5))
        finally:
            st.close()
    # today (seeded run) has 3 NVDA prints before 11:00 ET; prior sessions had 1 by then -> 3x
    pace = live_board.flow_pace(engine, ["NVDA"], RUN, T0, CFG)["NVDA"]
    assert (pace.today, pace.normal, pace.sessions) == (3, 1, 3)
    assert pace.multiple == pytest.approx(3.0)


def test_words_follow_the_display_profile() -> None:
    price = PriceContext(ticker="NVDA", spot=100.0, spot_fetched_at=T0, prev_close=99.0,
                         prev_close_day=date(2026, 9, 23), atr=2.0, atr_sessions=30,
                         benchmark_move=0.0, state=CheckState.CHECKED_FOUND)
    gamma = {"NVDA": SimpleNamespace(iv_pct=0.85, next_earnings=date(2026, 10, 1))}
    pace = {"NVDA": live_board.FlowPace(today=6, normal=2, sessions=5)}
    rows = live_board.build_rows(["NVDA"], {}, {"NVDA": price}, pace, {}, gamma,
                                 {"NVDA": ("AL", "neden", "green")}, date(2026, 9, 24), CFG)
    r = rows[0]
    assert r["iv"].startswith("pahalı") and r["pace"].startswith("yoğun: normalin 3.0 katı")
    assert r["earnings"] == "01.10 (7 gün)" and r["earnings_warn"] is True
    assert r["bet"] == "kayda değer işlem yok"
    assert r["move_pct"] == pytest.approx(1.0101, abs=1e-3)


def test_cycle_puts_the_board_in_the_snapshot(engine: Engine) -> None:
    gamma: dict[str, Any] = {"NVDA": SimpleNamespace(iv_pct=0.2, next_earnings=None)}
    import asyncio

    from webapp.board.signals import BoardSignalReader
    from webapp.live_alpha.job import run_cycle

    from tests.unit import test_live_alpha_job as j

    client = FakeUW()
    row = asyncio.run(run_cycle(engine, BoardSignalReader(engine=engine), client, j.SETTINGS, j.BOARD,
                                j.COSTS, T0, gamma_source=lambda: gamma))
    board = {r["ticker"]: r for r in row.snapshot["board"]}
    assert board["NVDA"]["verdict"] == "AL" and board["NVDA"]["verdict_tone"] == "green"
    assert board["NVDA"]["iv"].startswith("ucuz")
    assert board["NVDA"]["bet"].startswith("yükselişe %100")
    assert "SPY" not in board or board["SPY"]["bet"] == "kayda değer işlem yok"


def test_board_failure_never_breaks_the_cycle(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("board read failed")

    monkeypatch.setattr(live_board, "flow_pace", _boom)
    row = _cycle(engine, FakeUW(), T0)
    assert row.snapshot["board"] == [] and row.snapshot["cards"][0]["recommendation"] == "BUY"
