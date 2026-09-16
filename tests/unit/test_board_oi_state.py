"""Phase 5.2.B4b: the board's reading of the T+1 confirmation and the catalyst chip.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B4 and §9 (the Açık
pozisyon row); decisions P10 and P13.

Pins:
  - every stored status maps to the evidence layer's four states, and that
    mapping agrees with the Turkish state ``oi_confirm`` reports for the same
    row (§9 parity);
  - no confirmation row is ``bilinmiyor (T+1 bekleniyor)``, never "not opening";
  - the render readers create their tables on a fresh database, normalise their
    keys and make no Unusual Whales call;
  - a never-fetched catalyst source reads ``bilinmiyor``, and a stored event
    inside the window reads ``var``;
  - the catalyst window ends at the dominant contract's expiry close, 16:00 ET
    in both DST states;
  - every string the row line can show passes ``ensure_clean`` (R-WD1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from webapp.board.alfa_page import build_opening_view, expiry_close
from webapp.board.catalysts import (
    CHIP_UNKNOWN,
    EXPIRED_WINDOW,
    M22_MAY_DIFFER,
    AlfaCatalyst,
    AlfaCatalystFetch,
    ensure_catalyst_tables,
    read_board_catalysts,
)
from webapp.board.db import make_engine
from webapp.board.evidence import OI_CONFIRM_STATES, STATE_LABELS, open_interest_family
from webapp.board.honesty import ensure_clean
from webapp.board.oi_confirm import (
    BOARD_STATES,
    NO_ROW_LABEL,
    STATUS_LABELS,
    AlfaOiConfirm,
    board_state,
    ensure_oi_confirm_tables,
    load_oi_confirm,
    oi_label,
    read_board_oi,
)
from webapp.board.settings import load_board_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_NOW = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)  # 11:00 ET
_TRADE_DATE = date(2026, 9, 15)
_EXPIRY = date(2026, 9, 18)
_SYMBOL = "AAA260918C00100000"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'oi.db'}")


def _store(engine: Engine, status: str, *, symbol: str = _SYMBOL) -> None:
    ensure_oi_confirm_tables(engine)
    with Session(engine) as session:
        session.add(AlfaOiConfirm(
            option_symbol=symbol, trade_date=_TRADE_DATE, ticker=symbol[:3], option_type="call",
            strike=100.0, expiry=_EXPIRY, flagged_size=40, status=status, created_at=_NOW,
            oi_t=97, oi_t1=118, t1_date=date(2026, 9, 16), delta_oi=21,
        ))
        session.commit()


# ---------------------------------------------------------------------------
# The mapping (§9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(STATUS_LABELS))
def test_every_status_maps_to_the_evidence_state_it_reports(engine: Engine, status: str) -> None:
    _store(engine, status)
    with Session(engine) as session:
        view = load_oi_confirm(session, _SYMBOL, _TRADE_DATE)
    assert view is not None
    state = board_state(view)
    assert state is not None
    # The board state and the Turkish state oi_confirm reports are the same reading.
    assert STATE_LABELS[OI_CONFIRM_STATES[state]] == view.evidence_state
    assert BOARD_STATES[view.status] == state


def test_no_row_is_awaiting_t1_not_a_verdict() -> None:
    assert board_state(None) is None
    assert oi_label(None) == NO_ROW_LABEL == "henüz doğrulanmadı"
    family = open_interest_family(None)
    assert family.state == "unknown"
    assert family.text == "Açık pozisyon: bilinmiyor (T+1 bekleniyor)"


def test_the_four_contract_labels_are_byte_for_byte() -> None:
    assert STATUS_LABELS["acilis"] == "açılış (T+1 OI teyitli)"
    assert STATUS_LABELS["kapanis"] == "kapanış (T+1 OI düştü)"
    assert STATUS_LABELS["bekliyor"] == STATUS_LABELS["arada"] == "henüz doğrulanmadı"
    assert STATUS_LABELS["kapsam_disi"] == "kapsam-dışı (T+1'den önce vade)"


# ---------------------------------------------------------------------------
# The render readers
# ---------------------------------------------------------------------------


def test_read_board_oi_creates_its_table_on_a_fresh_database(engine: Engine) -> None:
    assert read_board_oi(engine, [(_SYMBOL, _TRADE_DATE)]) == {}
    assert "alfa_oi_confirm" in inspect(engine).get_table_names()


def test_read_board_oi_normalises_its_keys(engine: Engine) -> None:
    _store(engine, "acilis")
    rows = read_board_oi(engine, [(f"  {_SYMBOL.lower()} ", _TRADE_DATE), ("", _TRADE_DATE)])
    assert list(rows) == [(_SYMBOL, _TRADE_DATE)]
    assert rows[(_SYMBOL, _TRADE_DATE)].status == "acilis"
    assert read_board_oi(engine, []) == {}


def test_a_never_fetched_catalyst_source_reads_unknown(engine: Engine) -> None:
    chips = read_board_catalysts(
        engine, [("AAA", expiry_close(_EXPIRY))], settings=_SETTINGS, now=_NOW,
    )
    chip = chips[("AAA", expiry_close(_EXPIRY))]
    assert chip.text == "Vade içinde katalizör: Kazanç: bilinmiyor · FDA: bilinmiyor · Makro: bilinmiyor"
    assert chip.in_window is False
    assert "alfa_catalyst" in inspect(engine).get_table_names()


def _seed_earnings(engine: Engine, ticker: str = "AAA") -> None:
    ensure_catalyst_tables(engine)
    with Session(engine) as session:
        session.add(AlfaCatalyst(
            ticker=ticker, kind="earnings", when_key="2026-09-17", title="Q3",
            starts_at=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 18, 4, 0, tzinfo=UTC),
            precision="day", timing="postmarket", estimated=False, detail=None, fetched_at=_NOW,
        ))
        session.add(AlfaCatalystFetch(
            source="earnings", ticker=ticker, last_attempt_at=_NOW, last_status="ok",
            last_success_at=_NOW,
        ))
        session.commit()


def test_a_stored_event_inside_the_window_reads_var(engine: Engine) -> None:
    _seed_earnings(engine)
    wide = expiry_close(_EXPIRY)
    narrow = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)  # the window closes before the report
    chips = read_board_catalysts(
        engine, [("AAA", wide), ("AAA", narrow)], settings=_SETTINGS, now=_NOW,
    )
    assert chips[("AAA", wide)].in_window is True
    assert "Kazanç: 17.09 kapanış sonrası" in chips[("AAA", wide)].text
    assert chips[("AAA", narrow)].in_window is False
    assert "Kazanç: yok" in chips[("AAA", narrow)].text
    assert read_board_catalysts(engine, [], settings=_SETTINGS, now=_NOW) == {}


# ---------------------------------------------------------------------------
# The window and the row view
# ---------------------------------------------------------------------------


def test_the_window_ends_at_the_expiry_close_in_both_dst_states() -> None:
    assert expiry_close(_EXPIRY) == datetime(2026, 9, 18, 20, 0, tzinfo=UTC)  # 16:00 EDT
    assert expiry_close(date(2027, 1, 15)) == datetime(2027, 1, 15, 21, 0, tzinfo=UTC)  # 16:00 EST


def test_an_expired_contract_is_out_of_scope_not_no_catalyst() -> None:
    view = build_opening_view(None, None, expired=True)
    assert view.catalyst_text == EXPIRED_WINDOW == "Vade içinde katalizör: kapsam-dışı (vade geçti)"
    assert view.catalyst_in_window == ()
    assert view.catalyst_dimmed is True


def test_an_unread_chip_is_unknown_not_no_catalyst() -> None:
    view = build_opening_view(None, None, expired=False)
    assert view.catalyst_text == CHIP_UNKNOWN == "Vade içinde katalizör: bilinmiyor"
    assert view.catalyst_dimmed is True
    assert view.label == NO_ROW_LABEL
    assert view.status == "yok"
    assert view.state is None
    assert view.dimmed is True


@pytest.mark.parametrize(
    ("status", "dimmed"),
    [("acilis", False), ("kapanis", False), ("bekliyor", True), ("kapsam_disi", True)],
)
def test_only_a_confirmed_reading_is_not_dimmed(engine: Engine, status: str, dimmed: bool) -> None:
    _store(engine, status)
    row = read_board_oi(engine, [(_SYMBOL, _TRADE_DATE)])[(_SYMBOL, _TRADE_DATE)]
    view = build_opening_view(row, None, expired=False)
    assert view.dimmed is dimmed
    assert view.label == STATUS_LABELS[status]
    assert view.status == status


def test_the_catalyst_labels_feed_the_counter_argument(engine: Engine) -> None:
    _seed_earnings(engine)
    chip = read_board_catalysts(
        engine, [("AAA", expiry_close(_EXPIRY))], settings=_SETTINGS, now=_NOW,
    )[("AAA", expiry_close(_EXPIRY))]
    view = build_opening_view(None, chip, expired=False)
    assert view.catalyst_in_window == ("Kazanç",)  # a frozen kind label, never a vendor title
    assert view.catalyst_dimmed is True  # FDA and macro were never fetched


# ---------------------------------------------------------------------------
# R-WD1
# ---------------------------------------------------------------------------


def test_every_generated_string_is_clean(engine: Engine) -> None:
    _store(engine, "acilis")
    _seed_earnings(engine, "AAA")
    row = read_board_oi(engine, [(_SYMBOL, _TRADE_DATE)])[(_SYMBOL, _TRADE_DATE)]
    chip = read_board_catalysts(
        engine, [("AAA", expiry_close(_EXPIRY))], settings=_SETTINGS, now=_NOW,
    )[("AAA", expiry_close(_EXPIRY))]
    view = build_opening_view(row, chip, expired=False)
    texts = [
        *STATUS_LABELS.values(), NO_ROW_LABEL, EXPIRED_WINDOW, CHIP_UNKNOWN, M22_MAY_DIFFER,
        view.label, view.catalyst_text, *view.catalyst_in_window,
    ]
    for text in texts:
        assert ensure_clean(text) == text


def test_a_stale_window_never_claims_a_catalyst(engine: Engine) -> None:
    _seed_earnings(engine)
    # A window that ended before the report: "yok" inside that window, never "var".
    past = _NOW - timedelta(days=1)
    chip = read_board_catalysts(engine, [("AAA", past)], settings=_SETTINGS, now=_NOW)[("AAA", past)]
    assert chip.in_window is False
