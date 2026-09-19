"""Phase 5.3.4: the frozen card carries the spot frame the page showed.

Contract: ``docs/phase-5.3-spot-frame-acceptance.md`` §5 (the decision card) and
§6.9 (card parity).

``cards.snapshot`` is generic over dataclasses and ``build_card_view`` names no
field, so the frame 5.3.3 hung off ``AlfaRowView`` is frozen with no code change
at all. That is exactly why this file exists: a change that costs nothing is also
a change nobody would notice breaking.

The existing parity test compares ``frozen["row_view"]`` against
``cards.snapshot(view)`` — snapshot against snapshot. It would pass unchanged if
every spot cell froze as ``None`` on both sides. So the pins here are different:

  * the frozen numbers are compared against **the strings the page rendered**, not
    against another snapshot of the same object;
  * the frame is asserted to be populated first, so the comparison cannot be
    satisfied by two empty frames agreeing with each other (P39/P40);
  * a row whose frame was refused freezes its **reason**, so a card opened later
    says why it has no stop rather than showing a blank where a number belongs.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from webapp.board.atm import AlfaAtm, ensure_atm_tables
from webapp.board.daily_close import AlfaDailyBar, ensure_daily_bar_tables
from webapp.board.settings import load_board_settings

from tests.unit import _alfa_card_harness as harness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_DB = "card-spot.db"
_EXPIRY = date(2026, 9, 18)  # the harness seeds 2 DTE off 2026-09-16
_SPOT_CELLS = ("entry", "stop", "shares", "target", "atr")


def _seed_spot(url: str) -> None:
    """A fresh ATM row and enough bars for SPY; NVDA deliberately gets neither.

    SPY: entry $99, bars of range 10 -> ATR 10, so the stop sits 1.5 x 10 below the
    entry and $100 of R buys 6 shares. NVDA has no ATM row at all, so its frame is
    refused and must freeze the reason instead of a number.
    """
    engine = create_engine(url)
    try:
        ensure_atm_tables(engine)
        ensure_daily_bar_tables(engine)
        start = date(2026, 8, 1)
        with Session(engine) as session:
            session.add(
                AlfaAtm(
                    ticker="SPY", expiry=_EXPIRY, strike=100.0, stock_price=99.0,
                    call_bid=1.00, call_ask=1.10, call_iv=0.40,
                    put_bid=0.90, put_ask=1.00, put_iv=0.40,
                    trade_date=harness.TS.date(),
                    fetched_at=harness.NOW - timedelta(seconds=41),
                ),
            )
            session.add_all(
                AlfaDailyBar(
                    ticker="SPY", day=start + timedelta(days=i), open=100.0,
                    high=105.0, low=95.0, close=100.0, fetched_at=harness.NOW,
                )
                for i in range(_SETTINGS.spot.atr_min_sessions + 5)
            )
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType]]:
    """The shared card board, with the spot inputs seeded before the first render."""
    opened = harness.open_board(tmp_path, monkeypatch, name=_DB)
    client, module = next(opened)
    _seed_spot(f"sqlite:///{tmp_path / _DB}")
    try:
        yield client, module
    finally:
        next(opened, None)


def _row(body: str, ticker: str) -> str:
    found = re.search(rf'<article[^>]*data-ticker="{ticker}".*?</article>', body, re.S)
    assert found is not None, ticker
    return found.group(0)


def _cell(fragment: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", fragment)
    return None if found is None else html.unescape(found.group(1))


def _rendered(client: TestClient, ticker: str) -> dict[str, str | None]:
    row = _row(client.get("/", params={"gate": "off"}).text, ticker)
    return {name: _cell(row, f"data-spot-{name}") for name in _SPOT_CELLS}


def _frozen(module: ModuleType) -> tuple[dict[str, Any], dict[str, Any]]:
    (card,) = module._card_repo().list_cards()
    view = card.card["row_view"]
    return view["spot"], view["spot_text"]


# ---------------------------------------------------------------------------
# parity against what the page actually rendered
# ---------------------------------------------------------------------------


def test_the_frozen_card_holds_a_populated_frame(board: tuple[TestClient, ModuleType]) -> None:
    """The vacuity guard: assert numbers exist BEFORE comparing anything to them."""
    client, module = board
    assert harness.press(client, "pas").status_code == 303

    frame, _text = _frozen(module)
    assert frame["entry"] == pytest.approx(99.0)
    assert frame["stop"] == pytest.approx(84.0)          # 99 - 1.5 x ATR 10
    assert frame["stop_distance"] == pytest.approx(15.0)
    assert frame["shares"] == 6                           # floor(100 / 15)
    assert frame["risked_usd"] == pytest.approx(90.0)
    assert frame["risk_usd"] == pytest.approx(100.0)
    assert frame["atr"] == pytest.approx(10.0)
    assert frame["reason"] is None
    assert frame["sessions_used"] == _SETTINGS.spot.atr_min_sessions + 4


def test_the_frozen_strings_are_the_strings_the_page_showed(
    board: tuple[TestClient, ModuleType],
) -> None:
    """§6.9: card parity, checked across the two representations, not within one.

    The page renders ``spot_text``; the card freezes it. Comparing the card against
    another snapshot of the same object would prove only that snapshot is a
    function — this compares the JSON against the HTML a human read.
    """
    client, module = board
    rendered = _rendered(client, "SPY")
    assert rendered["entry"] == "giriş $99.00", "the fixture must render a real frame"

    assert harness.press(client, "pas").status_code == 303
    _frame, text = _frozen(module)

    for name in _SPOT_CELLS:
        assert text[name] == rendered[name], name
    assert text["known"] is True
    assert text["reason"] is None


def test_a_refused_frame_freezes_its_reason_rather_than_a_blank(
    board: tuple[TestClient, ModuleType],
) -> None:
    """NVDA has no ATM row: R-SP8 refuses the frame, and the card must say so.

    A card that stored nulls without the reason would leave the owner, months
    later, unable to tell a missing stop from a stop of zero.
    """
    client, module = board
    assert harness.press(client, "pas", ticker="NVDA", direction="down").status_code == 303

    frame, text = _frozen(module)
    assert frame["reason"] == "stale_spot"
    assert frame["entry"] is None
    assert frame["stop"] is None
    assert frame["shares"] is None
    assert text["known"] is False
    assert text["reason"] is not None

    rendered = _rendered(client, "NVDA")
    assert rendered["stop"] == text["stop"]
    assert rendered["shares"] == text["shares"]


def test_the_frame_survives_a_restart_inside_the_card(
    board: tuple[TestClient, ModuleType],
) -> None:
    """§5: a card opened in three months still explains the decision in its own terms.

    The repositories are dropped and rebuilt, so the second read comes from the
    database rather than from anything this process was holding.
    """
    client, module = board
    assert harness.press(client, "pas").status_code == 303
    (before,) = module._card_repo().list_cards()

    harness.reset_singletons(module)
    after = module._card_repo().get_card(before.id)
    assert after is not None
    assert after.card["row_view"]["spot"] == before.card["row_view"]["spot"]
    assert after.card["row_view"]["spot"]["shares"] == 6
    assert after.card["row_view"]["spot_text"]["entry"] == "giriş $99.00"


def test_the_frame_is_frozen_without_the_card_module_naming_it(
    board: tuple[TestClient, ModuleType],
) -> None:
    """``cards.py`` mentions no board field, and this is the property that relies on it.

    If someone replaces the generic walk with an explicit field list, the frame
    silently stops being frozen. This asserts the mechanism, not just the result.
    """
    client, module = board
    assert harness.press(client, "pas").status_code == 303

    source = (_REPO / "webapp" / "board" / "cards.py").read_text(encoding="utf-8")
    assert "spot" not in source, "cards.py should freeze the frame generically, by walking fields"

    frame, _text = _frozen(module)
    assert frame["entry"] is not None, "yet the frame is in the card"
