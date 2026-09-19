"""Phase 5.3.3: the spot frame on the rendered board (contract §3, R-SP1-R-SP8).

The seeded sqlite run of ``test_board_honesty`` is reused, with ``alfa_atm`` rows
for the entry price and ``alfa_daily_bar`` rows for the ATR — the first test to
write bars, because 5.3.1 stored them and nothing read them back until now.

Every ticker exists to pin one rule, in the contract's words:

  SPT  a complete frame: entry, ATR stop, share count floored to R, dollars risked
  CAP  R-SP6 — the position cap binds and the row says so
  FEW  R-SP1 — one short of ``atr_min_sessions``: no ATR, no stop, no count, and why
  STL  R-SP8 — the ATM row behind the entry is stale, so the whole frame is refused
  UNT  R-SP7 — an İŞLENMEZ row keeps its spot frame; the gate hides nothing

Also pinned: the option cost cells live inside the ``Opsiyon detayı`` fold while the
bull/bear grid stays outside it (a counter-argument the reader must open is not a
counter-argument, R-CA1), no forbidden words (R-WD1), and zero Unusual Whales calls.

The arithmetic itself is pinned in ``test_alfa_spot_frame.py`` by mutation; what is
pinned here is that the page states it, and states the reason when it cannot.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.alfa_page import SPOT_COPY
from webapp.board.atm import AlfaAtm, ensure_atm_tables
from webapp.board.daily_close import AlfaDailyBar, ensure_daily_bar_tables
from webapp.board.db import make_engine
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # test_board_honesty._TS
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)  # the seeded rows are 3 DTE
_QUOTE = (0.39, 0.41)
_WIDE = (0.10, 5.00)  # a spread no gate passes: İŞLENMEZ

_ROWS = (
    _Row("s1", "SPT", "call", "at_ask", "500000", 0.6137, _CONFIRMING, quote=_QUOTE),
    _Row("s2", "CAP", "call", "at_ask", "400000", 0.7248, _CONFIRMING, quote=_QUOTE),
    _Row("s3", "FEW", "call", "at_ask", "300000", 0.8319, _CONFIRMING, quote=_QUOTE),
    _Row("s4", "STL", "call", "at_ask", "200000", 0.9412, _CONFIRMING, quote=_QUOTE),
    _Row("s5", "UNT", "call", "at_ask", "100000", 0.5126, _CONFIRMING, quote=_WIDE),
)

# One short of the declared minimum, so R-SP1 is refused for the DECLARED reason and
# not merely because the ATR window is too short.
_TOO_FEW = _SETTINGS.spot.atr_min_sessions - 1


def _atm(ticker: str, price: float, **over: Any) -> AlfaAtm:
    fields: dict[str, Any] = {
        "ticker": ticker, "expiry": _EXPIRY, "strike": 100.0, "stock_price": price,
        "call_bid": 1.00, "call_ask": 1.10, "call_iv": 0.40,
        "put_bid": 0.90, "put_ask": 1.00, "put_iv": 0.40,
        "trade_date": _TS.date(), "fetched_at": _NOW - timedelta(seconds=41),
    }
    fields.update(over)
    return AlfaAtm(**fields)


def _bars(ticker: str, count: int, *, high: float, low: float, close: float) -> list[AlfaDailyBar]:
    """``count`` sessions whose True Range is exactly ``high - low`` after the first."""
    start = date(2026, 8, 1)
    return [
        AlfaDailyBar(
            ticker=ticker, day=start + timedelta(days=i), open=close,
            high=high, low=low, close=close, fetched_at=_NOW,
        )
        for i in range(count)
    ]


def _seed_spot(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_atm_tables(engine)
        ensure_daily_bar_tables(engine)
        enough = _SETTINGS.spot.atr_min_sessions + 5
        rows: list[Any] = [
            # Entry $99, ATR 10 -> distance 15, so 6 shares cost $594: the cap cannot bind.
            _atm("SPT", 99.0), *_bars("SPT", enough, high=105.0, low=95.0, close=100.0),
            # Entry $10, ATR 0.1 -> distance 0.15: 666 shares by R, $6,660, so the cap binds.
            _atm("CAP", 10.0), *_bars("CAP", enough, high=10.05, low=9.95, close=10.0),
            # The entry is known; the bars are one short of the declared minimum.
            _atm("FEW", 50.0), *_bars("FEW", _TOO_FEW, high=52.0, low=48.0, close=50.0),
            # Bars are plentiful, but the price behind the entry is two days old.
            _atm("STL", 99.0, fetched_at=_NOW - timedelta(days=2)),
            *_bars("STL", enough, high=105.0, low=95.0, close=100.0),
            # İŞLENMEZ on the option gate, with a perfectly good spot frame.
            _atm("UNT", 99.0), *_bars("UNT", enough, high=105.0, low=95.0, close=100.0),
        ]
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'spot.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    _seed_spot(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(row_html: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", row_html)
    return None if found is None else html.unescape(found.group(1))


def _board(client: TestClient) -> dict[str, str]:
    return _rows(client.get("/", params={"gate": "off"}).text)


def _fold(row_html: str) -> str:
    """The ``Opsiyon detayı`` fold of one row."""
    found = re.search(r"<details[^>]*data-option-detail.*?</details>", row_html, re.S)
    assert found is not None, "the row lost its option fold"
    return found.group(0)


# ---------------------------------------------------------------------------
# a complete frame
# ---------------------------------------------------------------------------


def test_a_complete_frame_states_entry_stop_size_and_risk(client: TestClient) -> None:
    """ATR 10 at the profile's 1.5x: stop $84, and $100 of R buys 6 shares, risking $90."""
    row = _board(client)["SPT"]
    assert 'data-spot="known"' in row
    assert _cell(row, "data-spot-entry") == "giriş $99.00"
    stop = _cell(row, "data-spot-stop")
    assert stop is not None
    assert stop.startswith("stop $84.00 · mesafe $15.00")
    assert _cell(row, "data-spot-shares") == "6 hisse · riske edilen $90.00 / $100.00"
    assert _cell(row, "data-spot-reason") is None
    assert _cell(row, "data-spot-capped") is None


def test_the_target_is_the_straddles_own_pricing_and_never_a_forecast(client: TestClient) -> None:
    """R-SP4: the target is the move moves.py already prices, labelled as the straddle's."""
    row = _board(client)["SPT"]
    target = _cell(row, "data-spot-target")
    assert target is not None
    assert re.fullmatch(r"hedef \$\d+\.\d{2} \(ATM straddle\) · -?\d+\.\dR", target), target
    assert _cell(row, "data-spot-disclosure") == SPOT_COPY["disclosure"]


def test_the_atr_cell_names_its_window_and_session_count(client: TestClient) -> None:
    row = _board(client)["SPT"]
    atr = _cell(row, "data-spot-atr")
    period = _SETTINGS.spot.atr_period
    assert atr == f"ATR({period}) $10.00 · {_SETTINGS.spot.atr_min_sessions + 4} seans"


# ---------------------------------------------------------------------------
# the binding rules
# ---------------------------------------------------------------------------


def test_r_sp6_the_bound_cap_is_disclosed_on_the_row(client: TestClient) -> None:
    """25% of $10,000 at $10 a share is 250 shares, not the 666 that R alone would buy."""
    row = _board(client)["CAP"]
    assert _cell(row, "data-spot-shares") == "250 hisse · riske edilen $37.50 / $100.00"
    capped = _cell(row, "data-spot-capped")
    assert capped is not None
    assert "%25" in capped


def test_r_sp1_too_few_bars_refuses_the_stop_and_says_why(client: TestClient) -> None:
    row = _board(client)["FEW"]
    assert 'data-spot="unknown"' in row
    assert _cell(row, "data-spot-reason") == SPOT_COPY["not_enough_bars"]
    assert _cell(row, "data-spot-stop") == SPOT_COPY["stop_unknown"]
    assert _cell(row, "data-spot-shares") == SPOT_COPY["shares_unknown"]
    # The entry is known even when the stop is not: only derived cells are refused.
    assert _cell(row, "data-spot-entry") == "giriş $50.00"
    assert f"{_TOO_FEW - 1} seans" in (_cell(row, "data-spot-atr") or "")


def test_r_sp8_a_stale_entry_price_refuses_the_whole_frame(client: TestClient) -> None:
    row = _board(client)["STL"]
    assert _cell(row, "data-spot-reason") == SPOT_COPY["stale_spot"]
    assert _cell(row, "data-spot-entry") == SPOT_COPY["entry_unknown"]
    assert _cell(row, "data-spot-stop") == SPOT_COPY["stop_unknown"]
    assert _cell(row, "data-spot-shares") == SPOT_COPY["shares_unknown"]


def test_r_sp7_the_option_gate_never_hides_a_spot_row(client: TestClient) -> None:
    """An İŞLENMEZ row keeps its frame, and the verdict stays readable inside the fold."""
    rows = _board(client)
    assert "UNT" in rows, "the gate removed a row from the spot view"
    row = rows["UNT"]
    assert _cell(row, "data-spot-entry") == "giriş $99.00"
    assert _cell(row, "data-spot-shares") == "6 hisse · riske edilen $90.00 / $100.00"
    assert "İŞLENMEZ" in html.unescape(_fold(row))


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------


def test_the_option_cost_cells_moved_inside_the_fold(client: TestClient) -> None:
    row = _board(client)["SPT"]
    fold = _fold(row)
    for marker in ("data-chip-label", "data-round-trip", "data-size-lot", "data-move-text"):
        assert marker in fold, marker
    assert SPOT_COPY["option_detail"] in html.unescape(fold)


def test_the_bull_and_bear_cases_stay_outside_the_fold(client: TestClient) -> None:
    """R-CA1: a counter-argument the reader has to open is not a counter-argument."""
    for ticker, row in _board(client).items():
        fold = _fold(row)
        assert "data-cases" not in fold, ticker
        assert "data-counter" not in fold, ticker
        assert "data-counter" in row, ticker
    # And the spot cells themselves are never folded away either.
    for ticker, row in _board(client).items():
        assert "data-spot-entry" not in _fold(row), ticker


# ---------------------------------------------------------------------------
# the standing guards
# ---------------------------------------------------------------------------


def test_the_spot_cell_marks_a_default_only_while_the_owner_has_not_confirmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P15: the share count rests on capital_usd and r_usd.

    5.3.5 recorded O3's confirmation in the profile, so the marker is gone by
    default and this test was inverted rather than dropped: the direction worth
    keeping is that an UNCONFIRMED capital figure still says so on a cell that
    sizes shares, which is the more misleading of the two places it could hide.
    """
    row = _board(client)["SPT"]
    assert _cell(row, "data-spot-shares") is not None, "the frame must render for this to mean anything"
    assert _cell(row, "data-spot-default") is None

    import webapp.main as m

    unconfirmed = _SETTINGS.model_copy(
        update={"sizing": _SETTINGS.sizing.model_copy(update={"values_confirmed_by_owner": False})},
    )
    monkeypatch.setattr(m, "_board_settings", lambda: unconfirmed)
    row = _board(client)["SPT"]
    assert _cell(row, "data-spot-default") == "(varsayılan değer)"


def test_the_spot_frame_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_frame_states_no_probability_and_never_says_buy_or_sell(client: TestClient) -> None:
    """R-SP4 and R-SP5, over every spot string the page can render."""
    rendered = " ".join(SPOT_COPY.values()).lower()
    for banned in ("olasılık", "ihtimal", "beklenen", "expected", "al ", "sat "):
        assert banned not in rendered, banned


def test_the_spot_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §6.10: the bars were already fetched; the frame costs the budget nothing."""
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/", params={"gate": "off"})
    assert response.status_code == 200
    assert "data-spot-entry" in response.text
    assert calls == []


def test_every_row_carries_a_frame_or_a_reason(client: TestClient) -> None:
    """No row is silent about the spot units: it states the frame, or why it cannot."""
    reasons: Sequence[str] = tuple(
        SPOT_COPY[key] for key in ("not_enough_bars", "no_stop_distance", "stale_spot")
    )
    for ticker, row in _board(client).items():
        assert "data-spot=" in row, ticker
        if 'data-spot="unknown"' in row:
            assert _cell(row, "data-spot-reason") in reasons, ticker
        else:
            assert _cell(row, "data-spot-shares") is not None, ticker
