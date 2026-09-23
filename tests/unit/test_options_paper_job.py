"""The options PAPER tracker job (Phase 5.24): register, mark, decide, heartbeat."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session
from webapp.board import daily_jobs as dj
from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
from webapp.board.db import make_engine
from webapp.board.options_paper import (
    AlfaOptPaperHeartbeat,
    AlfaOptPaperMark,
    AlfaOptPaperOutcome,
    read_tracker,
    run_options_paper,
)
from webapp.board.quota_ledger import reserved_on
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import UnusualWhalesTransientError

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
LONG, SHORT = "SPY261009C00775000", "SPY261009C00776000"
SESSIONS = [date(2026, 9, d) for d in (21, 22, 23, 24, 25, 28, 29, 30)]


def _card(tmp: Path) -> Path:
    root = tmp / "replay"
    (root / "2026-09-22").mkdir(parents=True)
    card = {"card": {
        "signal_id": "sig_replay_1", "opportunity_id": "opp", "hypothesis_id": "H00_pipeline_smoke",
        "research_status": "RESEARCH_ONLY", "data_origin": "replay", "session": "2026-09-22",
        "underlying": "SPY", "structure": "bull_call_debit",
        "net_debit_per_share": "0.58", "commission_usd": "2.60", "quantity": 1,
        "legs": [
            {"occ_symbol": LONG, "side": "long", "strike": "775", "expiry": "2026-10-09"},
            {"occ_symbol": SHORT, "side": "short", "strike": "776", "expiry": "2026-10-09"},
        ],
    }}
    (root / "2026-09-22" / "paper_card_SPY.json").write_text(json.dumps(card))
    # An outcome file sitting next to the card is not a card.
    (root / "2026-09-22" / "paper_card_SPY_outcome.json").write_text("{}")
    return root


class _Client:
    def __init__(self, rows: dict[str, list[dict[str, Any]]], fail: set[str] | None = None) -> None:
        self.rows = rows
        self.fail = fail or set()
        self.calls: list[str] = []
        self.last_daily_request_count: int | None = None

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        symbol = path.split("/")[3]
        self.calls.append(symbol)
        if symbol in self.fail:
            raise UnusualWhalesTransientError("boom")
        return {"chains": self.rows.get(symbol, [])}


def _rows(days: list[date], long_bid: str, short_ask: str) -> dict[str, list[dict[str, Any]]]:
    return {
        LONG: [{"date": d.isoformat(), "nbbo_bid": long_bid, "nbbo_ask": "9", "volume": 5} for d in days],
        SHORT: [{"date": d.isoformat(), "nbbo_bid": "0", "nbbo_ask": short_ask, "volume": 5} for d in days],
    }


@pytest.fixture
def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    eng = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    ensure_daily_close_tables(eng)
    with eng.begin() as conn:
        for d in SESSIONS:
            conn.execute(insert(AlfaDailyClose).values(
                ticker="SPY", day=d, close=700.0, fetched_at=datetime(2026, 9, 30, tzinfo=UTC)))
    return eng


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 22, 0, tzinfo=UTC)  # 18:00 ET, after the close


async def test_mid_hold_marks_without_an_outcome_and_labels_replay(engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _card(tmp_path)
    client = _Client(_rows([date(2026, 9, 23), date(2026, 9, 24)], "0.60", "0.02"))
    report = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(date(2026, 9, 24)), root=root,
    )
    assert report.registered == 1
    assert report.open_positions == 1
    assert report.marks_written == 2
    assert report.outcomes_written == 0
    assert report.requests_granted == 2 and sorted(client.calls) == sorted([LONG, SHORT])
    view = read_tracker(engine)
    (row,) = view.rows
    assert row.data_origin == "replay" and row.state == "acik"
    assert row.last_mark_session == date(2026, 9, 24) and row.last_mark_value == "0.58"
    assert view.runs == 1 and view.last_status == "ok"


async def test_a_full_horizon_writes_one_outcome_once(engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _card(tmp_path)
    days = [d for d in SESSIONS if d > date(2026, 9, 22)][:5]
    client = _Client(_rows(days, "0.66", "0.02"))
    first = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[-1]), root=root,
    )
    assert first.outcomes_written == 1
    second = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[-1]), root=root,
    )
    assert second.outcomes_written == 0 and second.open_positions == 0
    with Session(engine) as session:
        (outcome,) = session.scalars(select(AlfaOptPaperOutcome)).all()
        beats = session.scalars(select(AlfaOptPaperHeartbeat)).all()
    assert outcome.reason == "time" and outcome.realised
    assert outcome.pnl_usd == "3.40"  # (0.64 - 0.58) x 100 - 2.60
    assert json.loads(outcome.variants_json)["time_only"]["pnl_usd"] == "3.40"
    assert len(beats) == 2


async def test_an_unpriced_horizon_day_waits_one_session_then_is_unknown(engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _card(tmp_path)
    days = [d for d in SESSIONS if d > date(2026, 9, 22)]
    client = _Client(_rows(days[:4], "0.60", "0.02"))  # day 5 never gets a row
    waiting = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[4]), root=root,
    )
    assert waiting.outcomes_written == 0
    assert any("waiting one session" in n for n in waiting.notes)
    final = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[5]), root=root,
    )
    assert final.outcomes_written == 1
    view = read_tracker(engine)
    assert view.rows[0].state == "bilinmiyor"
    assert view.rows[0].pnl_usd is None


async def test_our_failed_fetch_writes_no_mark_and_no_outcome(engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _card(tmp_path)
    days = [d for d in SESSIONS if d > date(2026, 9, 22)][:5]
    client = _Client(_rows(days, "0.66", "0.02"), fail={SHORT})
    report = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[-1]), root=root,
    )
    assert report.status == "degraded"
    assert report.marks_written == 0 and report.outcomes_written == 0
    with engine.begin() as conn:
        assert conn.execute(select(AlfaOptPaperMark)).first() is None


async def test_a_refused_reservation_stops_spending(engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _card(tmp_path)
    days = [d for d in SESSIONS if d > date(2026, 9, 22)][:5]
    client = _Client(_rows(days, "0.66", "0.02"))
    cap = _SETTINGS.refresh.daily_request_soft_cap
    client.last_daily_request_count = cap  # the key is already at the cap
    report = await run_options_paper(
        client=client, engine=engine, settings=_SETTINGS, now=_at(days[-1]), root=root,
    )
    assert client.calls == []
    assert report.requests_refused == 1 and report.status == "quota_refused"
    assert reserved_on(engine, days[-1]) == cap
    assert read_tracker(engine).last_status == "quota_refused"


def test_the_tracker_is_in_the_registry_the_refresher_runs() -> None:
    settings: BoardSettings = _SETTINGS
    names = [j.name for j in dj.build_registry(settings)]
    assert "options_paper" in names
