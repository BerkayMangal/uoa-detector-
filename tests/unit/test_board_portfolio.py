"""Phase 5.2.B6a: portfolio overlap, capital header, single-bet clusters (webapp/board/portfolio.py)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from webapp.board import portfolio as pf
from webapp.board.etf_holdings import HoldingView
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.journal import TradeRow

_REPO = Path(__file__).resolve().parents[2]
_TODAY = date(2026, 9, 15)
_TS = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


def _trade(trade_id: str, ticker: str = "NVDA", **over: Any) -> TradeRow:
    fields: dict[str, Any] = {
        "id": trade_id, "created_at": _TS, "entry_ts": _TS, "ticker": ticker,
        "direction": "bullish", "instrument": "call", "strike": 220.0, "expiry": "2026-10-16",
        "contracts": 2.0, "entry_price": 4.10, "status": "open", "thesis": "",
    }
    fields.update(over)
    return TradeRow(**fields)


def _holding(etf: str, ticker: str, weight: float, updated: date = date(2026, 9, 11)) -> HoldingView:
    return HoldingView(etf=etf, ticker=ticker, updated=updated, weight_pct=weight,
                       sector="Technology", fetched_at=_TS)


_SMH = [
    _holding("SMH", "NVDA", 22.09), _holding("SMH", "TSM", 9.75), _holding("SMH", "AMD", 5.78),
    _holding("SMH", "AVGO", 5.83), _holding("SMH", "INTC", 2.10),
]


# ---------------------------------------------------------------------------
# journal parsing and overlap
# ---------------------------------------------------------------------------


def test_parse_trade_is_defensive() -> None:
    good = pf.parse_trade(_trade("a"), today=_TODAY)
    assert (good.direction, good.instrument, good.expiry, good.expired) == (
        "yukarı", "call", date(2026, 10, 16), False,
    )
    odd = pf.parse_trade(
        _trade("b", direction="long?", instrument="spread", expiry="16/10/2026",
               contracts=float("nan"), entry_price=-1.0, strike=None),
        today=_TODAY,
    )
    assert odd.direction is None and odd.instrument is None
    assert odd.expiry is None and odd.expired is None and odd.expiry_raw == "16/10/2026"
    assert odd.contracts is None and odd.entry_price is None and odd.strike is None
    past = pf.parse_trade(_trade("c", expiry="2026-06-26", direction=" Bearish "), today=_TODAY)
    assert past.direction == "aşağı" and past.expired is True


def test_badge_needs_an_open_trade_with_same_ticker_and_direction() -> None:
    trades = [
        _trade("open-up", ticker="nvda"),
        _trade("closed-up", status="closed"),
        _trade("open-down", direction="bearish", instrument="put"),
        _trade("other", ticker="AMD"),
        _trade("unknown-dir", direction="?"),
    ]
    up = pf.journal_overlap(trades, ticker="NVDA", direction="yukarı", today=_TODAY)
    assert up.badge == "zaten bu bahittesin"
    assert [t.trade_id for t in up.matched] == ["open-up"]
    assert [t.trade_id for t in up.unparsed_same_ticker] == ["unknown-dir"]
    assert up.note == pf.UNPARSED_DIRECTION_NOTE
    assert up.details == ("NVDA call 220 16.10.2026 · 2 kontrat @ $4.10",)
    down = pf.journal_overlap(trades, ticker="NVDA", direction="aşağı", today=_TODAY)
    assert [t.trade_id for t in down.matched] == ["open-down"]
    none = pf.journal_overlap(trades, ticker="TSLA", direction="yukarı", today=_TODAY)
    assert none.badge is None and none.matched == () and none.note is None


def test_trade_detail_marks_expired_and_unknown_fields() -> None:
    expired = pf.parse_trade(_trade("x", expiry="2026-06-26"), today=_TODAY)
    assert pf.trade_detail_text(expired) == (
        "NVDA call 220 26.06.2026 (vadesi geçti) · 2 kontrat @ $4.10"
    )
    shares = pf.parse_trade(_trade("s", instrument="shares", strike=None, expiry=None,
                                   contracts=10.0, entry_price=100.0), today=_TODAY)
    assert pf.trade_detail_text(shares) == "NVDA hisse · 10 kontrat @ $100.00"
    broken = pf.parse_trade(_trade("b", instrument="?", expiry="soon"), today=_TODAY)
    assert pf.trade_detail_text(broken) == "NVDA bilinmiyor 220 vade bilinmiyor · 2 kontrat @ $4.10"


# ---------------------------------------------------------------------------
# capital header
# ---------------------------------------------------------------------------


def test_capital_header_sums_open_long_premium(settings: BoardSettings) -> None:
    trades = [
        _trade("a"),  # 2 x 4.10 x 100 = 820
        _trade("b", ticker="AMD", contracts=1.0, entry_price=2.5),  # 250
        _trade("c", ticker="SPY", instrument="shares", strike=None, expiry=None,
               contracts=10.0, entry_price=100.0),  # 1000
        _trade("d", status="closed", contracts=50.0),
        _trade("e", instrument="straddle"),
    ]
    header = pf.capital_header(trades, settings=settings, today=_TODAY)
    assert header.open_trades == 4
    assert header.at_risk_usd == pytest.approx(2070.0)
    assert header.capital_usd == settings.sizing.capital_usd
    assert header.at_risk_pct == pytest.approx(2070.0 / settings.sizing.capital_usd * 100)
    assert header.unparsed == ("e",)
    assert header.values_confirmed is True  # 5.3.5: the profile carries O3's confirmation
    assert header.text == (
        "Açıktaki prim riski: $2,070 · sermayenin %20.7"
        " · 1 açık işlem okunamadı (bilinmiyor)"
    )


def test_capital_header_marks_the_default_while_the_owner_has_not_confirmed(
    settings: BoardSettings,
) -> None:
    """5.3.5 inverted this test instead of deleting it.

    It used to confirm the values and assert the marker disappeared. The profile now
    ships confirmed, so that direction is the default every other test already
    exercises, and asserting it here would prove nothing. The direction still worth
    pinning is the one that protects the owner: an unconfirmed capital figure must
    say so on the page.
    """
    unconfirmed = settings.model_copy(update={
        "sizing": settings.sizing.model_copy(update={"values_confirmed_by_owner": False}),
    })
    header = pf.capital_header([], settings=unconfirmed, today=_TODAY)
    assert header.values_confirmed is False
    assert header.text == "Açıktaki prim riski: $0 · sermayenin %0.0 (varsayılan değer)"


# ---------------------------------------------------------------------------
# clusters
# ---------------------------------------------------------------------------


def test_semis_cluster_from_smh_weights(settings: BoardSettings) -> None:
    clusters = pf.single_bet_clusters(["NVDA", "amd", "TSM", "AAPL", "INTC"], _SMH, settings=settings)
    (semis,) = clusters
    assert semis.members == ("AMD", "NVDA", "TSM")  # INTC is below the weight cut, AAPL not held
    assert semis.label_etf == "SMH"
    assert semis.links[0].weights == (("AMD", 5.78), ("NVDA", 22.09), ("TSM", 9.75))
    assert semis.text == "Tek bahis: AMD, NVDA, TSM (SMH 11.09.2026 tarihli; her biri ≥ %3)"


def test_non_focused_and_broad_etfs_never_link(settings: BoardSettings) -> None:
    qqq = [_holding("QQQ", "NVDA", 8.4), _holding("QQQ", "AAPL", 7.8)]
    assert pf.single_bet_clusters(["NVDA", "AAPL"], qqq, settings=settings) == ()
    cap = settings.portfolio.max_focused_holdings
    broad = [_holding("XLE", f"T{i:03d}", 5.0) for i in range(cap + 1)]
    assert pf.single_bet_clusters(["T000", "T001"], broad, settings=settings) == ()


def test_share_classes_merge_and_weights_add(settings: BoardSettings) -> None:
    igv = [_holding("IGV", "GOOG", 2.0), _holding("IGV", "GOOGL", 1.5), _holding("IGV", "MSFT", 8.0)]
    (cluster,) = pf.single_bet_clusters(["goog", "MSFT"], igv, settings=settings)
    assert cluster.members == ("GOOGL", "MSFT")
    assert dict(cluster.links[0].weights)["GOOGL"] == pytest.approx(3.5)


def test_clusters_chain_across_etfs_and_are_deterministic(settings: BoardSettings) -> None:
    holdings = [
        *_SMH,
        _holding("IGV", "TSM", 4.0), _holding("IGV", "MSFT", 9.0),
        _holding("XBI", "VRTX", 6.0), _holding("XBI", "REGN", 5.0),
        _holding("XBI", "NVDA", 1.0),
    ]
    tickers = ["NVDA", "TSM", "MSFT", "VRTX", "REGN", "AAPL"]
    expected = pf.single_bet_clusters(tickers, holdings, settings=settings)
    assert [c.members for c in expected] == [("MSFT", "NVDA", "TSM"), ("REGN", "VRTX")]
    assert expected[0].label_etf == "SMH"  # 31.84 summed weight vs IGV 13.0
    assert [lk.etf for lk in expected[0].links] == ["IGV", "SMH"]
    rng = random.Random(7)
    for _ in range(5):
        shuffled_h = list(holdings)
        shuffled_t = list(tickers)
        rng.shuffle(shuffled_h)
        rng.shuffle(shuffled_t)
        assert pf.single_bet_clusters(shuffled_t, shuffled_h, settings=settings) == expected


def test_label_tie_breaks_alphabetically(settings: BoardSettings) -> None:
    holdings = [
        _holding("TAN", "ENPH", 5.0), _holding("TAN", "FSLR", 5.0),
        _holding("ARKK", "ENPH", 5.0), _holding("ARKK", "FSLR", 5.0),
    ]
    (cluster,) = pf.single_bet_clusters(["FSLR", "ENPH"], holdings, settings=settings)
    assert cluster.label_etf == "ARKK"


def test_latest_snapshot_per_etf_is_used(settings: BoardSettings) -> None:
    holdings = [
        _holding("SMH", "NVDA", 22.0, date(2026, 9, 4)), _holding("SMH", "AMD", 6.0, date(2026, 9, 4)),
        _holding("SMH", "NVDA", 22.0, date(2026, 9, 11)), _holding("SMH", "AMD", 1.0, date(2026, 9, 11)),
    ]
    assert pf.single_bet_clusters(["NVDA", "AMD"], holdings, settings=settings) == ()


def test_same_sector_is_a_weak_link_only(settings: BoardSettings) -> None:
    sectors = {"NVDA": "Technology", "aapl": "Technology", "JPM": "Financial Services",
               "GOOGL": "Communication Services", "META": "Communication Services", "X": None}
    links = pf.same_sector_links(["NVDA", "AAPL", "JPM", "GOOG", "META", "X"], sectors,
                                 settings=settings)
    assert [(lk.sector, lk.members) for lk in links] == [
        ("Communication Services", ("GOOGL", "META")),
        ("Technology", ("AAPL", "NVDA")),
    ]
    assert links[1].text == "Aynı sektör (zayıf bağ, kümeye katılmaz): AAPL, NVDA · Technology"
    # Sector never creates a single-bet cluster.
    assert pf.single_bet_clusters(["NVDA", "AAPL"], [], settings=settings) == ()


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_every_generated_portfolio_string_is_clean(settings: BoardSettings) -> None:
    trades = [
        _trade("a"), _trade("b", direction="?", instrument="?", expiry="?"),
        _trade("c", expiry="2026-01-01", instrument="put", direction="bearish"),
        _trade("d", instrument="shares", strike=None, expiry=None),
    ]
    texts = [
        pf.OVERLAP_BADGE, pf.UNKNOWN, pf.DEFAULT_VALUE_MARK, pf.UNPARSED_DIRECTION_NOTE,
        pf.CAPITAL_HEADER_TEMPLATE, pf.CAPITAL_UNPARSED_TEMPLATE, pf.TRADE_DETAIL_TEMPLATE,
        pf.EXPIRED_MARK, pf.EXPIRY_UNKNOWN, pf.CLUSTER_TEMPLATE, pf.CLUSTER_LINK_TEMPLATE,
        pf.SECTOR_LINK_TEMPLATE, *pf.INSTRUMENT_LABELS.values(),
    ]
    for direction in ("yukarı", "aşağı"):
        view = pf.journal_overlap(trades, ticker="NVDA", direction=direction, today=_TODAY)  # type: ignore[arg-type]
        texts.extend([t for t in (view.badge, view.note) if t])
        texts.extend(view.details)
    texts.extend(pf.trade_detail_text(pf.parse_trade(t, today=_TODAY)) for t in trades)
    texts.append(pf.capital_header(trades, settings=settings, today=_TODAY).text)
    texts.extend(c.text for c in pf.single_bet_clusters(["NVDA", "AMD", "TSM"], _SMH, settings=settings))
    texts.extend(s.text for s in pf.same_sector_links(
        ["NVDA", "AMD"], {"NVDA": "Technology", "AMD": "Technology"}, settings=settings,
    ))
    for text in texts:
        assert ensure_clean(text) == text
    assert "olasılık" not in " ".join(texts)
