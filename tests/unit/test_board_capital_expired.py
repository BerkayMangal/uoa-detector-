"""Phase 5.2.B-fix2: the capital header never counts an expired option as premium at risk.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B6 ("Capital header.
Sum of open long premium at risk") and §2 R-UN1 (an unknown or out-of-scope
reading is never painted as a clean number).

Review finding FB-H2: the header summed every open trade at its full entry
premium, expired legs included, on the same page that marks those legs
``(vadesi geçti)``. An option that has expired carries no premium at risk, so
the board's only account-level money number overstated it.

Pins:
  - an expired option leg is out of the at-risk sum and disclosed instead of
    silently dropped;
  - the disclosure names how many legs and how much premium it left out;
  - shares never expire, and a trade whose expiry cannot be read stays counted
    (unknown is not "expired");
  - the page-level header follows the same rule;
  - the new copy passes ``ensure_clean`` (R-WD1) and states no probability.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from webapp.board import portfolio as pf
from webapp.board.alfa_page import build_alfa_page
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.journal import TradeRow

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TODAY = date(2026, 9, 15)
_NOW = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
_TS = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


def _trade(trade_id: str, ticker: str = "NVDA", **over: Any) -> TradeRow:
    fields: dict[str, Any] = {
        "id": trade_id, "created_at": _TS, "entry_ts": _TS, "ticker": ticker,
        "direction": "bullish", "instrument": "call", "strike": 220.0, "expiry": "2026-10-16",
        "contracts": 2.0, "entry_price": 4.10, "status": "open", "thesis": "",
    }
    fields.update(over)
    return TradeRow(**fields)


@pytest.fixture
def settings() -> BoardSettings:
    return _SETTINGS


def test_an_expired_leg_is_left_out_of_the_at_risk_sum(settings: BoardSettings) -> None:
    live = _trade("live")  # 2 x $4.10 x 100 = $820, expires 16.10.2026
    expired = _trade("old", ticker="AMD", expiry="2026-06-25", contracts=1.0, entry_price=2.00)
    header = pf.capital_header([live, expired], settings=settings, today=_TODAY)
    assert header.open_trades == 2  # the expired trade is still an open journal row
    assert header.at_risk_usd == pytest.approx(820.0)
    assert header.expired_trades == 1
    assert header.expired_usd == pytest.approx(200.0)
    assert header.at_risk_pct == pytest.approx(820.0 / settings.sizing.capital_usd * 100)
    assert header.text == (
        "Açıktaki prim riski: $820 · sermayenin %8.2 (varsayılan değer)"
        " · 1 işlemin vadesi geçti ($200 hariç)"
    )


def test_without_an_expired_leg_the_header_is_unchanged(settings: BoardSettings) -> None:
    header = pf.capital_header([_trade("a")], settings=settings, today=_TODAY)
    assert header.expired_trades == 0
    assert header.expired_usd == pytest.approx(0.0)
    assert header.text == "Açıktaki prim riski: $820 · sermayenin %8.2 (varsayılan değer)"


def test_shares_never_expire_and_an_unreadable_expiry_stays_counted(
    settings: BoardSettings,
) -> None:
    shares = _trade("s", instrument="shares", strike=None, expiry=None,
                    contracts=10.0, entry_price=100.0)  # $1,000
    unreadable = _trade("u", expiry="soon", contracts=1.0, entry_price=1.00)  # $100
    header = pf.capital_header([shares, unreadable], settings=settings, today=_TODAY)
    assert header.expired_trades == 0
    assert header.at_risk_usd == pytest.approx(1100.0)


def test_an_expired_leg_is_disclosed_not_dropped(settings: BoardSettings) -> None:
    expired = _trade("old", expiry="2026-06-25", contracts=3.0, entry_price=2.00)
    header = pf.capital_header([expired], settings=settings, today=_TODAY)
    assert header.at_risk_usd == pytest.approx(0.0)
    assert header.expired_usd == pytest.approx(600.0)
    assert "1 işlemin vadesi geçti ($600 hariç)" in header.text
    assert "$0" in header.text  # the live risk is stated, never hidden


def test_the_rendered_page_header_follows_the_same_rule(settings: BoardSettings) -> None:
    trades = [_trade("live"), _trade("old", ticker="AMD", expiry="2026-06-25",
                                     contracts=1.0, entry_price=2.00)]
    page = build_alfa_page([], settings, now=_NOW, trades_source=lambda: trades)
    assert page.capital is not None
    assert page.capital.at_risk_usd == pytest.approx(820.0)
    assert page.capital.text.startswith("Açıktaki prim riski: $820")
    assert "1 işlemin vadesi geçti ($200 hariç)" in page.capital.text


def test_the_expired_disclosure_is_clean_and_states_no_probability(
    settings: BoardSettings,
) -> None:
    expired = _trade("old", expiry="2026-06-25", contracts=1.0, entry_price=2.00)
    texts = [
        pf.CAPITAL_EXPIRED_TEMPLATE,
        pf.capital_header([expired], settings=settings, today=_TODAY).text,
    ]
    for text in texts:
        assert ensure_clean(text) == text
    joined = " ".join(texts).lower()
    for banned in ("olasılık", "ihtimal", "beklenen", "kâr"):
        assert banned not in joined, banned
