"""The daily workflow cannot promote source-only, old or unrecorded entries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.desk.overview import build
from webapp.live_alpha import store
from webapp.opening import storage
from webapp.opening.model import Advice, clock
from webapp.opening.view import Statement, View

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import source

NOW = datetime(2026, 10, 28, 13, 45, tzinfo=UTC)


def opening(now: datetime = NOW, statements: tuple[Statement, ...] = ()) -> View:
    return View(clock(now), now, True, now, True, statements, (), (), (), 0, "", date(2026, 10, 28))


def statement(action: str = "BUY", **changes: object) -> Statement:
    advice = Advice.model_validate(
        {
            "ticker": "NVDA",
            "action": action,
            "headline": "NVDA kayıtlı plan",
            "reasons": ("Fiyat teyidi bekleniyor.",),
            "expires_at": NOW + timedelta(minutes=5),
        }
        | changes
    )
    return Statement("key", advice, NOW, "scan", True)


@pytest.mark.parametrize(
    ("minutes", "text", "next_time"),
    [
        (-60, "Hazırlık saatini bekle", "13:10"),
        (-25, "Listeyi hazırla", "13:45"),
        (-10, "İlk 15 dakikanın", "13:45"),
        (0, "Şu an yeni giriş planı yok", None),
        (400, "Yeni giriş zamanı değil", None),
    ],
)
def test_wait_has_a_specific_reason_and_calendar_checkpoint(
    minutes: int, text: str, next_time: str | None
) -> None:
    now = NOW + timedelta(minutes=minutes)
    result = build(opening(now), now)
    assert result.kind == "wait" and text in result.title
    assert (result.next_at.strftime("%H:%M") if result.next_at else None) == next_time
    assert result.buys == ()


def test_current_entry_is_displayed_with_its_own_expiry() -> None:
    row = statement()
    result = build(opening(statements=(row,)), NOW)
    assert result.kind == "plan" and result.buys == (row,)
    assert build(opening(statements=(row,)), NOW + timedelta(minutes=5)).buys == ()
    assert build(opening(statements=(row,)), NOW + timedelta(minutes=5)).kind == "wait"


@pytest.mark.parametrize("changed", ["cycle", "statement", "expiry", "settling"])
def test_old_or_unconfirmed_entry_never_acquires_a_plan_headline(changed: str) -> None:
    row = statement()
    value = opening(statements=(row,))
    if changed == "cycle":
        value = replace(value, current=False)
    elif changed == "statement":
        value = replace(value, statements=(replace(row, current=False),))
    elif changed == "expiry":
        value = replace(value, statements=(statement(expires_at=None),))
    else:
        value = replace(value, clock=clock(NOW - timedelta(minutes=1)))
    result = build(value, NOW)
    assert result.buys == () and result.kind == "wait"
    assert "giriş planı var" not in result.title


@pytest.mark.parametrize("action", ["SELL", "PAPER_EXIT", "REVIEW"])
def test_held_risk_precedes_a_new_entry_without_conflating_scopes(action: str) -> None:
    exit_row = statement(action, scope="paper" if action == "PAPER_EXIT" else "manual")
    result = build(opening(statements=(statement(), exit_row)), NOW)
    assert result.kind == "risk" and result.exits == (exit_row,)
    assert result.buys[0].advice.action == "BUY"
    assert result.exits[0].advice.scope == exit_row.advice.scope


def test_stale_sell_cannot_be_promoted_but_review_remains_an_inspection() -> None:
    value = replace(opening(statements=(statement("SELL"),)), current=False)
    assert build(value, NOW).kind == "risk"
    assert build(value, NOW).buys == ()
    review = replace(statement("REVIEW"), current=False)
    result = build(replace(value, statements=(review,)), NOW)
    assert result.kind == "risk" and result.exits == (review,)
    assert "kontrol et" in result.title


@pytest.mark.parametrize(
    ("recorded", "current", "text"),
    [(False, False, "Henüz kayıtlı"), (True, False, "Güncel karar")],
)
def test_unrecorded_and_old_data_are_explicit(recorded: bool, current: bool, text: str) -> None:
    value = replace(opening(), recorded=recorded, current=current)
    result = build(value, NOW)
    assert text in result.title and result.kind == "wait"


def test_unavailable_data_and_holiday_do_not_invent_a_next_session_time() -> None:
    now = datetime(2026, 12, 25, 15, tzinfo=UTC)
    result = build(None, now)
    assert result.record_label == "Kayıtlar okunamadı"
    assert result.next_at is None and result.clock.opening is None and result.buys == ()


def test_wait_reasons_are_candidate_counts_and_deterministic_on_ties() -> None:
    first = statement("WAIT", reasons=("A", "A", "B"))
    second = statement("WAIT", reasons=("B", "C", "D"))
    result = build(opening(statements=(first, second)), NOW)
    assert result.reasons == (("B", 2), ("A", 1), ("C", 1))


def test_home_does_not_promote_a_legacy_buy_or_write_new_advice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'overview.db'}")
    store.ensure_live_tables(engine)
    storage.ensure(engine)
    row = source()
    store.write_scan(engine, row, row.snapshot["policy_version"])
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    mutations: list[str] = []

    def executed(
        _c: object, _cursor: object, sql: str, _parameters: object, _ctx: object, _many: bool
    ) -> None:
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            mutations.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    try:
        response = TestClient(main.app).get("/", headers=set_web_auth(monkeypatch))
        assert response.status_code == 200
        hero = response.text.split('id="opening-desk"', 1)[1].split("</section>", 1)[0]
        assert "Henüz kayıtlı karar yok" in hero and "PLAN VAR" not in hero
        assert 'data-card="NVDA" data-rec="BUY"' in response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert mutations == []
    finally:
        engine.dispose()
