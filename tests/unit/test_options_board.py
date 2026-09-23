"""Phase 5.24 M7: the /opsiyon screen's reader.

The rules worth protecting here are honesty rules, not formatting ones. A board
that cannot read its artifacts must say so rather than render a calm empty page,
and a card that disagrees with the engine that wrote it must surface that
disagreement instead of displaying it as fact.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from webapp.options_board import load_board

from tests.unit._webapp_auth import authed_client

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SCOPE = "artifacts/options-alpha-v1"


def _card_payload(**overrides: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "session": "2026-09-22",
        "underlying": "SPY",
        "direction": "up",
        "structure": "bull_call_debit",
        "research_status": "RESEARCH_ONLY",
        "opportunity_status": "WATCH",
        "data_origin": "replay",
        "quality_tier": "B",
        "quantity": 1,
        "entry_cost_usd": "58.00",
        "structural_max_loss_usd": "60.60",
        "max_profit_usd": "39.40",
        "target_usd": "39.40",
        "planned_stop_usd": "30.30",
        "invalidation": "5 islem gunu doldu",
        "trigger": "hacim 7496 > acik pozisyon 1805",
        "counter_argument": "Bu tetigin dogrulanmis bir edge'i YOK.",
        "missing_data": ["kotasyon zaman damgasi yok"],
        "legs": [
            {
                "occ_symbol": "SPY261009C00775000",
                "right": "call",
                "strike": "775",
                "expiry": "2026-10-09",
                "multiplier": 100,
                "side": "long",
                "bid": "7.26",
                "ask": "7.29",
                "quote_as_of": "2026-09-22",
            },
        ],
    }
    card.update(overrides)
    return {"card": card}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_unreadable_scope_says_so_and_never_looks_like_a_quiet_day(tmp_path: Path) -> None:
    board = load_board(tmp_path)

    # The distinction that matters: "I could not look" is not "there was nothing".
    assert board.state == "load-failed"
    assert board.state != "no-cards"
    assert board.cards == ()
    assert board.problems, "okunamayan kapsam sessiz kalamaz"


def test_a_readable_scope_with_no_cards_is_a_different_state(tmp_path: Path) -> None:
    (tmp_path / _SCOPE / "replay").mkdir(parents=True)

    board = load_board(tmp_path)

    assert board.state == "no-cards"
    assert board.scope_readable is True


def test_card_and_its_outcome_are_read_together(tmp_path: Path) -> None:
    card_path = tmp_path / _SCOPE / "replay/2026-09-08/paper_card_SPY.json"
    _write(card_path, _card_payload(session="2026-09-08"))
    _write(
        card_path.with_name("paper_card_SPY_outcome.json"),
        {
            "exits": {
                "time_only": {
                    "reason": "time",
                    "exit_day": "2026-09-15",
                    "pnl_usd": "-30.60",
                    "held_trading_days": 5,
                    "unpriced_days": 0,
                },
                "time_and_stop": {
                    "reason": "stop",
                    "exit_day": "2026-09-11",
                    "pnl_usd": "-25.00",
                    "held_trading_days": 3,
                    "unpriced_days": 0,
                },
            },
        },
    )

    board = load_board(tmp_path)

    assert len(board.cards) == 1
    row = board.cards[0]
    assert row.underlying == "SPY"
    assert row.is_scored is True
    assert row.primary_outcome is not None
    # The primary is the frozen one, never whichever variant looks better.
    assert row.primary_outcome.variant == "time_only"
    assert row.primary_outcome.pnl_usd == "-30.60"
    assert [other.variant for other in row.other_outcomes] == ["time_and_stop"]
    assert board.closed_count == 1
    assert str(board.closed_pnl_usd) == "-30.60"


def test_an_unscored_card_is_not_counted_as_a_result(tmp_path: Path) -> None:
    _write(tmp_path / _SCOPE / "replay/2026-09-22/paper_card_QQQ.json", _card_payload())

    board = load_board(tmp_path)

    assert board.cards[0].is_scored is False
    assert board.closed_count == 0
    assert board.closed_pnl_usd is None


def test_a_replay_card_claiming_entry_ready_is_reported_as_a_contradiction(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / _SCOPE / "replay/2026-09-22/paper_card_SPY.json",
        _card_payload(data_origin="replay", opportunity_status="PAPER_ENTRY_READY"),
    )

    board = load_board(tmp_path)

    contradictions = board.cards[0].contradictions
    assert contradictions, "replay karti giris hazir olarak sunulamaz"
    assert any("WATCH" in item for item in contradictions)


def test_a_target_above_the_structure_ceiling_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path / _SCOPE / "replay/2026-09-22/paper_card_SPY.json",
        _card_payload(target_usd="60.60", max_profit_usd="39.40", quantity=1),
    )

    board = load_board(tmp_path)

    assert any("ulaşılamaz" in item for item in board.cards[0].contradictions)


def test_a_target_within_the_ceiling_raises_nothing(tmp_path: Path) -> None:
    _write(
        tmp_path / _SCOPE / "replay/2026-09-22/paper_card_SPY.json",
        _card_payload(target_usd="39.40", max_profit_usd="39.40", quantity=1),
    )

    assert load_board(tmp_path).cards[0].contradictions == ()


def test_only_milestones_carrying_a_verdict_are_research_rows(tmp_path: Path) -> None:
    _write(
        tmp_path / _SCOPE / "run_manifest.json",
        {
            "milestones": [
                {"id": "M0", "name": "API capability matrix", "status": "DONE"},
                {
                    "id": "M5",
                    "name": "H03",
                    "verdict": "REJECTED",
                    "headline": "confirmed arm lost more",
                },
                {
                    "id": "M5b",
                    "name": "H01",
                    "verdict": "REJECTED",
                    "headline": "two clauses failed",
                    "verdict_failed_clauses": ["not cost positive", "interval spans zero"],
                },
            ],
        },
    )
    (tmp_path / _SCOPE / "replay").mkdir(parents=True)

    board = load_board(tmp_path)

    assert [row.milestone for row in board.research] == ["M5", "M5b"]
    assert board.research[1].failed_clauses == ("not cost positive", "interval spans zero")


def test_a_corrupt_card_is_a_reported_problem_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / _SCOPE / "replay/2026-09-22/paper_card_SPY.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    board = load_board(tmp_path)

    assert board.cards == ()
    assert any("okunamadı" in problem for problem in board.problems)


# --- the route ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'webapp.db'}")
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests

    import webapp.main as m

    m._REPO = m._JOURNAL = m._GAMMA = None
    yield authed_client(m.app, monkeypatch)
    m._REPO = m._JOURNAL = m._GAMMA = None


def test_the_screen_renders_against_the_repository_artifacts(client: TestClient) -> None:
    response = client.get("/opsiyon")

    assert response.status_code == 200
    body = response.text
    # Reads the real committed artifacts, so it must not fall back to "could not read".
    assert 'data-state="ok"' in body
    assert 'data-state="load-failed"' not in body


def test_the_screen_never_sells_rejected_research_as_signal(client: TestClient) -> None:
    body = client.get("/opsiyon").text

    assert "REJECTED" in body
    assert "reddedildi" in body
    assert "RESEARCH_ONLY" in body
    # The counter-argument travels with every card and is not an optional footnote.
    assert "Karşı argüman" in body


# --- Phase 5.24: corrected verdicts and the live tracker ---------------------


def test_a_corrected_verdict_is_shown_with_what_it_replaced(client: TestClient) -> None:
    """The v2 rerun (AUDIT_H10_TRADES.md) moved H04 from REJECTED to
    INSUFFICIENT_DATA. The screen shows the new verdict AND the old one."""
    body = client.get("/opsiyon").text
    assert "data-corrected" in body
    assert "v1: REJECTED" in body
    assert "INSUFFICIENT_DATA" in body
    assert "M5e" in body  # H02 is on the verdict table
    assert "tamamı reddedildi" not in body


class _Reader:
    def __init__(self, engine: object) -> None:
        self.engine = engine


def test_a_tracker_that_never_ran_says_so(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.main as m
    from webapp.board.db import make_engine

    monkeypatch.setattr(m, "_BOARD_READER", _Reader(make_engine(f"sqlite:///{tmp_path / 't.db'}")))
    body = client.get("/opsiyon").text
    assert 'data-tracker="never-ran"' in body


async def test_a_tracked_position_renders_with_its_heartbeat(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as _json
    from datetime import UTC, date, datetime

    import webapp.main as m
    from sqlalchemy import insert
    from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
    from webapp.board.db import make_engine
    from webapp.board.options_paper import run_options_paper
    from webapp.board.settings import load_board_settings

    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    ensure_daily_close_tables(engine)
    with engine.begin() as conn:
        for d in (22, 23, 24):
            conn.execute(insert(AlfaDailyClose).values(
                ticker="SPY", day=date(2026, 9, d), close=1.0, fetched_at=datetime(2026, 9, 24, tzinfo=UTC)))
    root = tmp_path / "replay" / "2026-09-22"
    root.mkdir(parents=True)
    (root / "paper_card_SPY.json").write_text(_json.dumps({"card": {
        "signal_id": "sig_x", "data_origin": "replay", "hypothesis_id": "H00", "research_status": "RESEARCH_ONLY",
        "session": "2026-09-22", "underlying": "SPY", "structure": "bull_call_debit",
        "net_debit_per_share": "0.58", "commission_usd": "2.60", "quantity": 1,
        "legs": [{"occ_symbol": "L", "side": "long", "strike": "775", "expiry": "2026-10-09"},
                 {"occ_symbol": "S", "side": "short", "strike": "776", "expiry": "2026-10-09"}],
    }}))

    class _Client:
        last_daily_request_count = None

        async def request_json(self, path: str, **_: object) -> dict[str, object]:
            symbol = path.split("/")[3]
            price = {"L": ("0.61", "0.70"), "S": ("0.00", "0.03")}[symbol]
            return {"chains": [{"date": f"2026-09-{d}", "nbbo_bid": price[0], "nbbo_ask": price[1]}
                               for d in (23, 24)]}

    await run_options_paper(
        client=_Client(), engine=engine, settings=load_board_settings(),
        now=datetime(2026, 9, 24, 22, 0, tzinfo=UTC), root=tmp_path / "replay",
    )
    monkeypatch.setattr(m, "_BOARD_READER", _Reader(engine))
    body = client.get("/opsiyon").text
    assert 'data-tracker="alive"' in body
    assert 'data-tracked="acik"' in body
    assert "2026-09-24 &middot; 0.58" in body
