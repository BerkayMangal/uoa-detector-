"""PAPER marks are time-scoped; closed accounting and complete history stay intact."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp import main
from webapp.board.db import AlfaBase, make_engine
from webapp.journal import TradeRow
from webapp.live_alpha import page, store

from tests.unit._webapp_auth import set_web_auth

NOW = datetime(2026, 7, 1, 15, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'paper-visibility.db'}")
    AlfaBase.metadata.create_all(engine)
    cast(Table, TradeRow.__table__).create(engine, checkfirst=True)
    page.ensure_ready(engine)
    yield engine
    page._ready_engines.discard(id(engine))
    engine.dispose()


def add(engine: Engine, key: str, **changes: Any) -> None:
    values: dict[str, Any] = {
        "paper_id": key,
        "opportunity_id": f"2026-07-01:{key}:up",
        "rec_id": key,
        "ticker": key.upper(),
        "direction": "up",
        "instrument": "stock",
        "plan_json": store.dumps({"policy_version": "live_alpha_v1", "stock_plan": {"stop": 97, "target": 106}}),
        "state": "PAPER_OPEN",
        "created_at": NOW - timedelta(minutes=10),
        "entry_at": NOW - timedelta(minutes=5),
        "entry_price": 100.05,
        "quantity": 10,
        "last_mark": 100.,
        "last_mark_at": NOW,
    }
    values.update(changes)
    assert store.create_paper(engine, store.LivePaper(**values))


def projected(engine: Engine, key: str, now: datetime = NOW) -> dict[str, Any]:
    view = page.build_view(engine, now)
    return next(row for row in view.papers_open + view.papers_closed if row["paper_id"] == key)


@pytest.mark.parametrize(
    ("age", "expected"),
    [(0, "current"), (900, "current"), (901, "stale"), (-1, "future")],
)
def test_sqlite_mark_currentness_uses_existing_freshness_and_nonfuture_clock(
    engine: Engine, age: int, expected: str,
) -> None:
    add(engine, "freshness", entry_at=NOW - timedelta(hours=1), last_mark_at=NOW - timedelta(seconds=age))
    row = projected(engine, "freshness")
    assert row["mark_status"] == expected
    assert row["recorded_mark"] == 100.
    assert row["current_mark"] == (100. if expected == "current" else None)
    assert row["last_mark_at"].tzinfo is not None
    assert row["last_mark_at"].utcoffset() == timedelta(0)
    assert isinstance(row["mark_text"], str) and row["mark_text"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"last_mark_at": None}, "missing_time"),
        ({"last_mark": None}, "missing"),
        ({"last_mark": float("inf")}, "invalid"),
        ({"last_mark": -1.}, "invalid"),
        ({"last_mark": 0.}, "invalid"),
        ({"last_mark_at": NOW - timedelta(minutes=6)}, "invalid_time"),
    ],
)
def test_sqlite_missing_invalid_and_preentry_mark_cannot_look_current(
    engine: Engine, changes: dict[str, object], expected: str,
) -> None:
    add(engine, "unknown", **changes)
    row = projected(engine, "unknown")
    assert row["mark_status"] == expected and row["current_mark"] is None
    if expected in {"invalid", "missing"}:
        assert row["recorded_mark"] is None


def test_nan_projection_is_unknown_even_when_adapter_returns_a_python_nan(engine: Engine) -> None:
    # SQLite stores IEEE NaN as NULL, so exercise the existing persistence adapter's
    # Python-value boundary directly rather than pretending SQLite preserves NaN.
    add(engine, "nan")
    record = store.papers(engine)[0]
    record.last_mark = math.nan
    row = page._paper_dict(record, NOW)
    assert row["mark_status"] == "invalid"
    assert row["current_mark"] is row["recorded_mark"] is None


def test_previous_et_day_mark_is_stale_even_with_a_recent_wall_clock(engine: Engine) -> None:
    at = datetime(2026, 7, 2, 4, 5, tzinfo=UTC)
    add(engine, "old_day", created_at=at - timedelta(hours=1), entry_at=at - timedelta(hours=1),
        last_mark_at=at - timedelta(minutes=10))
    row = projected(engine, "old_day", at)
    assert row["mark_status"] == "stale" and row["current_mark"] is None
    assert row["recorded_mark"] == 100.


def test_real_zero_option_mark_remains_a_priced_current_reference(engine: Engine) -> None:
    add(engine, "zero", instrument="long_call", entry_price=2., quantity=1, last_mark=0.)
    row = projected(engine, "zero")
    assert row["recorded_mark"] == row["current_mark"] == 0.
    assert row["mark_status"] == "current" and row["category"] == "open"


def test_saved_clocks_are_aware_utc_and_render_full_london_date(engine: Engine) -> None:
    add(engine, "clock")
    row = projected(engine, "clock")
    assert row["entry_at"] == NOW - timedelta(minutes=5)
    assert row["last_mark_at"] == NOW and row["exit_at"] is None
    assert "01.07.2026" in row["entry_time"] and "15:55" in row["entry_time"]
    assert "01.07.2026" in row["last_mark_time"] and "16:00" in row["last_mark_time"]
    assert row["entry_time"] == "01.07.2026 15:55:00"
    assert row["last_mark_time"] == "01.07.2026 16:00:00"


def test_closed_historical_reference_never_becomes_current_or_rewrites_exit_pnl(engine: Engine) -> None:
    entry, exit_ = NOW - timedelta(days=3), NOW - timedelta(days=2)
    add(engine, "closed", state="CLOSED_SIMULATED", instrument="long_call", entry_at=entry,
        entry_price=2., quantity=1, last_mark=0., last_mark_at=exit_, exit_at=exit_,
        exit_price=0., pnl_usd=-201.3, exit_reason="süre doldu")
    row = projected(engine, "closed")
    assert row["mark_status"] == "historical" and row["current_mark"] is None
    assert row["recorded_mark"] == 0. and row["category"] == "closed"
    assert (row["entry_price"], row["exit_price"], row["pnl_usd"]) == (2., 0., -201.3)
    assert row["entry_at"] == entry and row["exit_at"] == exit_
    assert row["exit_time"] == "29.06.2026 16:00:00"


def test_all_history_counts_and_recent_twenty_nonactive_rows_are_distinct(engine: Engine) -> None:
    # These 25 historical records are deliberately mixed. The oldest five are
    # losses; none should disappear from all-history accounting when UI slices 20.
    for n in range(25):
        pnl = -10. if n < 5 else 10. if n % 2 else 0.
        add(engine, f"history{n:02}", state="CLOSED_SIMULATED", created_at=NOW - timedelta(days=30-n),
            entry_at=NOW - timedelta(days=29-n), exit_at=NOW - timedelta(days=1),
            last_mark_at=NOW - timedelta(days=1), exit_price=101., pnl_usd=pnl)
    add(engine, "unfilled", state="CLOSED_SIMULATED", created_at=NOW - timedelta(minutes=4),
        entry_at=None, entry_price=None, quantity=None, last_mark=None, last_mark_at=None,
        exit_at=NOW, exit_reason="dolmadı")
    add(engine, "unresolved", state="UNRESOLVED", created_at=NOW - timedelta(minutes=3),
        entry_at=NOW - timedelta(minutes=2), exit_at=NOW,
        exit_reason="çıkış fiyatı yok")
    add(engine, "open", created_at=NOW - timedelta(minutes=2), entry_at=NOW - timedelta(minutes=1))
    add(engine, "pending", created_at=NOW - timedelta(minutes=1), state="PAPER_PENDING", entry_at=None,
        entry_price=None, quantity=None, last_mark=None, last_mark_at=None)
    view = page.build_view(engine, NOW)
    stats = view.paper_stats
    assert len(view.papers_closed) == 20 and len(view.papers_open) == 2
    assert (stats["total"], stats["closed"], stats["wins"], stats["losses"], stats["flats"]) == (29, 25, 10, 5, 10)
    assert (stats["unfilled"], stats["unknown"], stats["pnl_usd"]) == (1, 1, 50.)
    assert (stats["recent_limit"], stats["recent_total"], stats["recent_closed"]) == (20, 20, 18)
    assert (stats["recent_wins"], stats["recent_losses"], stats["recent_flats"]) == (9, 0, 9)
    assert (stats["recent_unfilled"], stats["recent_unknown"]) == (1, 1)
    unfilled = next(row for row in view.papers_closed if row["paper_id"] == "unfilled")
    assert unfilled["category"] == "unfilled" and unfilled["state_text"] == "PAPER: dolmadı"


def test_missing_outcomes_are_not_closed_losses_or_zero_aggregate_pnl(engine: Engine) -> None:
    add(engine, "unfilled", state="CLOSED_SIMULATED", entry_at=None, entry_price=None, quantity=None,
        exit_at=NOW, last_mark=None, last_mark_at=None)
    add(engine, "unresolved", state="UNRESOLVED", exit_at=NOW)
    add(engine, "exit_pending", state="EXIT_SIGNALLED", exit_at=NOW)
    stats = page.build_view(engine, NOW).paper_stats
    assert (stats["total"], stats["closed"], stats["losses"], stats["wins"], stats["flats"]) == (3, 0, 0, 0, 0)
    assert stats["pnl_usd"] is None
    assert stats["unfilled"] == stats["unknown"] == 1
    assert stats["recent_closed"] == stats["recent_losses"] == 0


def test_private_html_marks_recent_scope_zero_prices_and_clocks_without_get_writes(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(engine, "fresh")
    add(engine, "stale", last_mark_at=NOW - timedelta(seconds=901), entry_at=NOW - timedelta(hours=1))
    add(engine, "zero", instrument="long_call", entry_price=2., quantity=1, last_mark=0.)
    add(engine, "closed", state="CLOSED_SIMULATED", instrument="long_call", entry_price=2., quantity=1,
        created_at=NOW - timedelta(days=3), entry_at=NOW - timedelta(days=2),
        exit_at=NOW - timedelta(days=1), exit_price=0., pnl_usd=-201.3,
        last_mark_at=NOW - timedelta(days=1), last_mark=0.)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    headers = set_web_auth(monkeypatch)
    with Session(engine) as session:
        before = [(p.paper_id, p.entry_price, p.exit_price, p.pnl_usd, p.last_mark, p.last_mark_at)
                  for p in session.scalars(select(store.LivePaper).order_by(store.LivePaper.paper_id))]
    writes: list[str] = []

    def record_write(_connection: object, _cursor: object, sql: str, *_args: object) -> None:
        if sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP")):
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", record_write)
    client = TestClient(main.app)
    assert client.get("/").status_code == 401
    response = client.get("/", headers=headers)
    assert response.status_code == 200
    body = response.text
    rows = {ticker: text for ticker, text in re.findall(r'<tr[^>]*data-paper="([^"]+)"[^>]*>(.*?)</tr>', body, re.S)}
    assert set(rows) == {"FRESH", "STALE", "ZERO", "CLOSED"}
    assert "data-paper-current-mark" in rows["FRESH"] and "data-paper-current-mark" in rows["ZERO"]
    assert "data-paper-current-mark" not in rows["STALE"] and "data-paper-current-mark" not in rows["CLOSED"]
    assert re.search(r'>\s*0(?:\.0+)?(?:\s|<)', rows["ZERO"])
    assert re.search(r'>\s*0(?:\.0+)?(?:\s|<)', rows["CLOSED"])
    assert "01.07.2026" in rows["FRESH"] and "Europe/London" in body
    assert "data-paper-all-history" in body and "data-paper-recent" in body
    assert 'href="/sonuclar"' in body
    assert not writes
    with Session(engine) as session:
        after = [(p.paper_id, p.entry_price, p.exit_price, p.pnl_usd, p.last_mark, p.last_mark_at)
                 for p in session.scalars(select(store.LivePaper).order_by(store.LivePaper.paper_id))]
    assert after == before
