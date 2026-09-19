"""Phase 5.2.A-fix1: cost cells never come from an unexecutable quote (review FA-01, FA-02).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO1 ("Cost always
uses real prices: entry at ask, exit at bid") and §5 A2 ("kotasyon yok ... is an
unknown state").

Pins:
  - a crossed quote (ask below bid) reads ``kotasyon yok`` with its raw bid and
    ask, and spread, round trip and lot cost read unknown; no cell renders a
    negative dollar or percent;
  - a 0/0 NBBO reads ``İŞLENMEZ`` (bid 0) with round trip and lot cost unknown,
    never a ``$0`` entry price or a commission-only round trip;
  - bid 0 with a positive ask keeps its real cost: entry at ask, exit at 0.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board.alfa_page import chip_text, load_spread_cutoff_pct
from webapp.board.settings import load_board_settings
from webapp.board.tradability import QuoteView, TradabilityRead, assess_tradability

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)


def _assess(bid: float, ask: float) -> TradabilityRead:
    quote = QuoteView(
        option_symbol="CRS260918C00100000", nbbo_bid=bid, nbbo_ask=ask, volume=500,
        last_tape_time=None, fetched_at=_NOW - timedelta(seconds=41), returned=True,
    )
    return assess_tradability(
        "CRS", quote, None,
        tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=_NOW,
    )


def test_crossed_quote_shows_its_prices_but_no_cost() -> None:
    read = _assess(1.10, 1.00)
    assert (read.state, read.reason) == ("no_quote", "CRS kotasyonu tutarsız (ask bid'in altında)")
    assert (read.bid, read.ask) == (1.10, 1.00)
    assert (read.spread_pct, read.round_trip_usd, read.lot_cost_usd, read.lot_pct_capital) == (
        None, None, None, None,
    )
    assert read.quote_age_seconds == 41
    text = chip_text(read)
    assert (text.spread, text.round_trip, text.lot) == ("bilinmiyor", "bilinmiyor", "bilinmiyor")
    assert text.bid_ask == "bid $1.10 / ask $1.00"


def test_zero_nbbo_has_no_entry_price_and_no_round_trip() -> None:
    read = _assess(0.0, 0.0)
    assert (read.state, read.label, read.reason) == ("untradable", "İŞLENMEZ", "CRS bid 0, çıkış fiyatı yok")
    assert (read.spread_pct, read.round_trip_usd, read.lot_cost_usd, read.lot_pct_capital) == (
        None, None, None, None,
    )
    text = chip_text(read)
    assert (text.round_trip, text.lot) == ("bilinmiyor", "bilinmiyor")


def test_zero_bid_with_a_real_ask_keeps_its_real_cost() -> None:
    read = _assess(0.0, 0.05)
    assert read.state == "untradable"
    commission = _SETTINGS.cost.commission_per_contract_usd
    assert read.round_trip_usd == pytest.approx(0.05 * 100 + 2 * commission)
    assert read.lot_cost_usd == pytest.approx(5.0)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'cost.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    _reset(m)
    rows = (
        _Row("c1", "CRS", "call", "at_ask", "300000", 0.41, _CONFIRMING, quote=(1.10, 1.00)),
        _Row("c2", "ZZZ", "call", "at_ask", "200000", 0.42, _CONFIRMING, quote=(0.0, 0.0)),
    )
    _seed(url, rows, gamma_row=False)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def test_rendered_cost_cells_are_never_negative_or_a_zero_entry(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert set(rows) == {"CRS", "ZZZ"}
    for ticker, row in rows.items():
        # The chip strip, bounded by its OWN markup. This used to slice from
        # "data-chip=" to "data-cases"; 5.3.3 moved the bull/bear grid above the chip
        # (P43), which would have made the slice empty and every assertion below
        # vacuously true. The chip div contains spans only, so its first </div> closes it.
        chip_div = re.search(r'data-chip="[^"]*">.*?</div>', row, re.S)
        assert chip_div is not None, ticker
        chip = html.unescape(chip_div.group(0))
        assert "$-" not in chip, ticker
        assert "%-" not in chip, ticker
        for cell in ("data-round-trip", "data-lot"):
            found = re.search(rf"{cell}>([^<]*)<", chip)
            assert found is not None, (ticker, cell)
            assert found.group(1) == "bilinmiyor", (ticker, cell)
    assert "kotasyon yok" in html.unescape(rows["CRS"])
    assert "İŞLENMEZ" in html.unescape(rows["ZZZ"])
    assert "$0 ·" not in html.unescape(rows["ZZZ"])
