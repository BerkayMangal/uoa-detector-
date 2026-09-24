"""Phase 5.25 Live Alpha: the pure engine (docs/phase-5.25-live-alpha-acceptance.md §11).

Arithmetic examples are worked by hand in the comments, so a reader can check each
number without running anything.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from webapp.board.direction import direction_for

from uoa_detector.live_alpha.calendar import MarketMode, classify, sessions_between
from uoa_detector.live_alpha.decide import build_card, evaluate
from uoa_detector.live_alpha.flow import dedupe, qualifying_direction, side_direction, summarise
from uoa_detector.live_alpha.model import (
    CheckState,
    FlowPrint,
    NewsCheck,
    NewsItem,
    OptionQuote,
    Path,
    PriceContext,
    Readiness,
    Recommendation,
    to_jsonable,
)
from uoa_detector.live_alpha.option_plan import (
    build_structures,
    candidate_strikes,
    choose_instrument,
    occ_symbol,
    pick_expiry,
    price_view,
    quote_blocker,
)
from uoa_detector.live_alpha.settings import _REPO, load_live_alpha_settings
from uoa_detector.live_alpha.stock_plan import build_stock_plan
from uoa_detector.options_alpha.settings import load_settings as load_options_settings

S = load_live_alpha_settings()
COSTS = load_options_settings(_REPO / "profiles" / "options_alpha_v1.yaml")
LIVE_NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)      # Thu 11:00 ET
EXPIRY = date(2026, 10, 16)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "mode"),
    [
        (datetime(2026, 9, 24, 14, 0, tzinfo=UTC), MarketMode.LIVE),        # 10:00 EDT
        (datetime(2026, 9, 24, 12, 0, tzinfo=UTC), MarketMode.PREMARKET),   # 08:00 EDT
        (datetime(2026, 9, 24, 20, 0, tzinfo=UTC), MarketMode.CLOSED),      # 16:00 EDT, the close
        (datetime(2026, 9, 24, 7, 0, tzinfo=UTC), MarketMode.CLOSED),       # 03:00 EDT
        (datetime(2026, 9, 26, 15, 0, tzinfo=UTC), MarketMode.CLOSED),      # Saturday
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), MarketMode.CLOSED),     # Thanksgiving
        (datetime(2026, 11, 27, 17, 59, tzinfo=UTC), MarketMode.LIVE),      # 12:59 EST early-close day
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), MarketMode.CLOSED),     # 13:00 EST early close
        (datetime(2026, 11, 2, 14, 29, tzinfo=UTC), MarketMode.PREMARKET),  # 09:29 EST after DST ended
        (datetime(2026, 11, 2, 14, 30, tzinfo=UTC), MarketMode.LIVE),       # 09:30 EST
        (datetime(2026, 10, 30, 13, 30, tzinfo=UTC), MarketMode.LIVE),      # 09:30 EDT before DST ended
        (datetime(2028, 3, 1, 15, 0, tzinfo=UTC), MarketMode.DEGRADED),     # year not in the profile
    ],
)
def test_session_modes(moment: datetime, mode: MarketMode) -> None:
    assert classify(moment, S.calendar).mode is mode


def test_holiday_reason_and_next_session() -> None:
    s = classify(datetime(2026, 11, 26, 15, 0, tzinfo=UTC), S.calendar)
    assert s.reason == "tatil"
    assert s.next_session == date(2026, 11, 27)


def test_sessions_between_skips_weekend_and_holiday() -> None:
    # Wed 25 Nov -> Mon 30 Nov: Thu 26 is Thanksgiving, so Fri 27 and Mon 30 count.
    assert sessions_between(date(2026, 11, 25), date(2026, 11, 30), S.calendar) == 2


def test_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        classify(datetime(2026, 9, 24, 15, 0), S.calendar)


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


def _print(i: int, *, ticker: str = "NVDA", option_type: str = "call", side: str | None = "at_ask",
           premium: str = "100000", chain: str | None = None, seconds: int | None = None,
           strike: str = "185") -> FlowPrint:
    return FlowPrint(
        event_id=f"e{i}", ticker=ticker,
        ts=datetime(2026, 9, 24, 14, 0, tzinfo=UTC) + timedelta(seconds=seconds if seconds is not None else i * 120),
        option_type=option_type, strike=Decimal(strike), expiry=EXPIRY, premium=Decimal(premium),
        fill_side=side, option_chain=chain or f"{ticker}261016C{i:08d}",
    )


@pytest.mark.parametrize("side", ["at_ask", "above_ask", "at_bid", "below_bid"])
@pytest.mark.parametrize("option_type", ["call", "put"])
def test_side_direction_matches_the_board_rule(option_type: str, side: str) -> None:
    assert side_direction(option_type, side) == direction_for(option_type, side).direction


@pytest.mark.parametrize("side", ["midpoint", "unknown", None])
def test_unknown_side_has_no_direction(side: str | None) -> None:
    assert side_direction("call", side) is None


def test_same_contract_within_a_minute_is_one_print() -> None:
    a = _print(1, chain="NVDA261016C00185000", seconds=0)
    b = _print(2, chain="NVDA261016C00185000", seconds=30)
    c = _print(3, chain="NVDA261016C00185000", seconds=200)
    merged = dedupe([a, b, c], 60)
    assert len(merged) == 2
    assert merged[0].premium == Decimal("200000")


def _qualifying_up() -> list[FlowPrint]:
    # 3 distinct bought calls, 100k each = 300k up; one sold call 50k = 50k down.
    return [_print(1), _print(2), _print(3), _print(4, side="at_bid", premium="50000")]


def test_flow_qualifies_up() -> None:
    summary = summarise("NVDA", "live-2026-09-24", _qualifying_up(), S.flow)
    assert summary.premium_up == 300000 and summary.premium_down == 50000
    assert qualifying_direction(summary, S.flow) == ("up", "")


def test_mixed_flow_does_not_qualify() -> None:
    prints = [_print(1), _print(2), _print(3), _print(4, option_type="put", premium="250000")]
    d, why = qualifying_direction(summarise("NVDA", "r", prints, S.flow), S.flow)
    assert d is None and "karışık" in why


def test_one_contract_is_not_enough() -> None:
    prints = [_print(i, chain="NVDA261016C00185000", seconds=i * 300, premium="150000") for i in range(3)]
    d, why = qualifying_direction(summarise("NVDA", "r", prints, S.flow), S.flow)
    assert d is None and "farklı kontrat" in why


# ---------------------------------------------------------------------------
# Stock plan
# ---------------------------------------------------------------------------


def _price(spot: float = 100.0, prev: float = 99.0, atr: float = 2.0, bench: float | None = 0.0,
           fetched: datetime | None = None) -> PriceContext:
    return PriceContext(
        ticker="NVDA", spot=spot, spot_fetched_at=fetched or LIVE_NOW - timedelta(seconds=60),
        prev_close=prev, prev_close_day=date(2026, 9, 23), atr=atr, atr_sessions=30,
        benchmark_move=bench, state=CheckState.CHECKED_FOUND,
    )


def test_stock_plan_by_hand() -> None:
    # spot 100, prev 99, ATR 2: chase limit 99 + 1.0*2 = 101; stop 100 - 1.5*2 = 97;
    # target 100 + 2*3 = 106; shares min(floor(100/3)=33, floor(2500/100)=25) = 25.
    plan = build_stock_plan("up", _price(), S.price, S.stock_plan)
    assert plan is not None
    assert (plan.chase_limit, plan.stop, plan.target) == (101.0, 97.0, 106.0)
    assert (plan.entry_zone_low, plan.entry_zone_high) == (99.0, 101.0)
    assert plan.shares == 25 and plan.planned_risk_usd == 75.0 and plan.notional_usd == 2500.0
    assert "nakit tavanı" in plan.note
    assert "tahmin değil" in plan.target_label


def test_no_plan_without_atr() -> None:
    assert build_stock_plan("up", replace(_price(), atr=None), S.price, S.stock_plan) is None


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def _q(strike: str, bid: str | None, ask: str | None, *, right: str = "call", in_session: bool = True,
       fetched: datetime | None = None, multiplier: int = 100) -> OptionQuote:
    return OptionQuote(
        option_symbol=occ_symbol("NVDA", EXPIRY, right, float(strike)), underlying="NVDA", right=right,
        strike=Decimal(strike), expiry=EXPIRY, bid=Decimal(bid) if bid else None,
        ask=Decimal(ask) if ask else None, fetched_at=fetched or LIVE_NOW - timedelta(seconds=30),
        in_session=in_session, multiplier=multiplier,
    )


def test_occ_symbol() -> None:
    assert occ_symbol("nvda", EXPIRY, "call", 185) == "NVDA261016C00185000"
    assert occ_symbol("SPY", EXPIRY, "put", 662.5) == "SPY261016P00662500"


def test_pick_expiry_uses_the_dte_window() -> None:
    today = date(2026, 9, 24)
    assert pick_expiry([date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 16)], today, S.option_plan) == date(2026, 10, 16)
    assert pick_expiry([date(2026, 9, 25)], today, S.option_plan) is None


def test_candidate_strikes_are_capped_and_positive() -> None:
    strikes = candidate_strikes(100.0, 106.0, S.option_plan.strike_grid_candidates, 40)
    assert 0 < len(strikes) <= 40 and all(s > 0 for s in strikes)
    assert 100.0 in strikes and 105.0 in strikes


def test_bull_call_spread_by_hand() -> None:
    # entry debit = long ask 2.00 - short bid 0.50 = 1.50, x1.02 latency = 1.53
    # exit credit = long bid 1.90 - short ask 0.60 = 1.30, x0.98 = 1.274 -> 1.27
    # cost 153.00; commission 0.65 x 2 legs x 2 = 2.60; max loss 155.60 > R 100 -> 0 lots
    # round trip = ((1.53 - 1.27) x 100 + 2.60) / 153 = 28.60 / 153 = 18.7 %
    view = price_view("bull_call_debit", [(_q("100", "1.90", "2.00"), True), (_q("105", "0.50", "0.60"), False)],
                      LIVE_NOW, date(2026, 9, 24), S.option_plan, COSTS, S.stock_plan.r_usd)
    assert view.entry_debit == 1.53 and view.exit_credit == 1.27
    assert view.entry_cost_usd == 153.0 and view.commission_usd == 2.6 and view.max_loss_usd == 155.6
    assert view.roundtrip_cost_pct == 18.7
    assert view.lots == 0 and view.readiness is Readiness.RISK_BLOCKED
    assert "155.60" in view.blocker
    assert view.max_profit_usd == pytest.approx(5 * 100 - 153 - 2.6)


def test_cheap_long_call_is_ready() -> None:
    # 0.60 ask x1.02 = 0.61 (0.612 rounds half-even to 0.61); cost 61 + 1.30 commission = 62.30 <= 100 -> 1 lot
    view = price_view("long_call", [(_q("110", "0.55", "0.60"), True)], LIVE_NOW, date(2026, 9, 24),
                      S.option_plan, COSTS, S.stock_plan.r_usd)
    assert view.readiness is Readiness.READY and view.lots == 1 and view.max_loss_usd == 62.3


def test_non_standard_multiplier_is_carried() -> None:
    view = price_view("long_call", [(_q("110", "0.55", "0.60", multiplier=10), True)], LIVE_NOW,
                      date(2026, 9, 24), S.option_plan, COSTS, S.stock_plan.r_usd)
    assert view.multiplier == 10 and view.entry_cost_usd == 6.1


def test_mixed_multipliers_are_invalid() -> None:
    view = price_view("bull_call_debit", [(_q("100", "1.9", "2.0"), True), (_q("105", "0.5", "0.6", multiplier=10), False)],
                      LIVE_NOW, date(2026, 9, 24), S.option_plan, COSTS, 1000)
    assert view.readiness is Readiness.INVALID


@pytest.mark.parametrize(
    ("quote", "fragment"),
    [
        (_q("100", "2.10", "2.00"), "çapraz"),
        (_q("100", "1.9", "2.0", in_session=False), "seans dışında"),
        (_q("100", "1.9", "2.0", fetched=LIVE_NOW - timedelta(hours=1)), "bayat"),
        (_q("100", None, None), "NBBO boş"),
    ],
)
def test_unexecutable_quotes_are_quote_pending(quote: OptionQuote, fragment: str) -> None:
    assert fragment in quote_blocker(quote, LIVE_NOW, S.option_plan)
    view = price_view("long_call", [(quote, True)], LIVE_NOW, date(2026, 9, 24), S.option_plan, COSTS, 1000)
    assert view.readiness is Readiness.QUOTE_PENDING


def test_missing_short_leg_is_reported() -> None:
    views = build_structures("up", {"a": _q("100", "1.9", "2.0")}, 100.0, 106.0, LIVE_NOW,
                             date(2026, 9, 24), S.option_plan, COSTS, 100)
    assert views[1].readiness is Readiness.INVALID and "kısa bacak" in views[1].blocker


def test_no_returned_contracts_is_invalid() -> None:
    views = build_structures("up", {}, 100.0, 106.0, LIVE_NOW, date(2026, 9, 24), S.option_plan, COSTS, 100)
    assert views[0].readiness is Readiness.INVALID


def test_instrument_choice() -> None:
    single = price_view("long_call", [(_q("110", "0.55", "0.60"), True)], LIVE_NOW, date(2026, 9, 24),
                        S.option_plan, COSTS, 100)
    wide = price_view("long_call", [(_q("100", "1.00", "2.00"), True)], LIVE_NOW, date(2026, 9, 24),
                      S.option_plan, COSTS, 1000)
    assert choose_instrument([single], S.option_plan, stock_available=True).preferred == "long_call"
    # 1.00 wide on a 2.04 debit: round trip far above 25 % -> the stock wins, with the reason.
    choice = choose_instrument([wide], S.option_plan, stock_available=True)
    assert choice.preferred == "stock" and "gidiş-dönüş" in choice.reason
    assert choose_instrument([wide], S.option_plan, stock_available=False).preferred == "none"


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _news(state: CheckState = CheckState.CHECKED_FOUND, age_hours: float = 2.0) -> NewsCheck:
    item = NewsItem(
        headline="NVDA wins a data-center contract", source="BusinessWire",
        provider_created_at=LIVE_NOW - timedelta(hours=age_hours), first_seen_at=LIVE_NOW,
        sentiment="positive", is_major=True, tags=("contract",), tickers=("NVDA",), ref="r1",
    )
    items = (item,) if state is CheckState.CHECKED_FOUND else ()
    return NewsCheck(ticker="NVDA", state=state, checked_at=LIVE_NOW, items=items,
                     detail="UW 429 hız sınırı" if state is CheckState.FAILED else "")


def _flow() -> object:
    return summarise("NVDA", "live-2026-09-24", _qualifying_up(), S.flow)


def _eval(price: PriceContext | None = None, news: NewsCheck | None = None, now: datetime = LIVE_NOW,
          flow: object | None = None) -> object:
    return evaluate(flow or _flow(), price or _price(), news or _news(), classify(now, S.calendar), now, S)  # type: ignore[arg-type]


def test_p1_news_continuation_reaches_buy_ready() -> None:
    d = _eval()
    assert d.recommendation is Recommendation.BUY and d.readiness is Readiness.READY  # type: ignore[attr-defined]
    assert d.path is Path.P1_NEWS_CONTINUATION  # type: ignore[attr-defined]


def test_p2_market_relative_reaches_buy_ready_without_news() -> None:
    # move +1.01 % vs SPY 0.0 % -> relative +1.01 % >= 0.5 %
    d = _eval(news=_news(CheckState.CHECKED_NONE))
    assert d.recommendation is Recommendation.BUY and d.path is Path.P2_MARKET_RELATIVE  # type: ignore[attr-defined]


def test_p3_extended_price_is_conditional_with_pullback_level() -> None:
    d = _eval(price=_price(spot=102.0))   # (102 - 99) / 2 = 1.5 ATR > 1.0
    assert d.recommendation is Recommendation.CONDITIONAL_BUY and d.path is Path.P3_PULLBACK  # type: ignore[attr-defined]
    assert d.readiness is Readiness.TRIGGER_PENDING  # type: ignore[attr-defined]
    assert d.stock_plan.entry_ref == 101.0  # type: ignore[attr-defined]


def test_p4_bearish_setup() -> None:
    prints = [_print(i, option_type="put") for i in range(1, 4)]
    flow = summarise("NVDA", "r", prints, S.flow)
    d = _eval(price=_price(spot=97.0), flow=flow)
    assert d.recommendation is Recommendation.BEARISH_SETUP and d.direction == "down"  # type: ignore[attr-defined]


def test_price_against_up_flow_is_avoid() -> None:
    d = _eval(price=_price(spot=98.0))    # -0.5 ATR
    assert d.recommendation is Recommendation.AVOID  # type: ignore[attr-defined]


def test_stale_spot_is_never_ready() -> None:
    d = _eval(price=_price(fetched=LIVE_NOW - timedelta(hours=1)))
    assert d.readiness is not Readiness.READY and d.recommendation is Recommendation.CONDITIONAL_BUY  # type: ignore[attr-defined]
    assert any("bayat" in b for b in d.blockers)  # type: ignore[attr-defined]


def test_closed_market_is_never_ready() -> None:
    closed = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)
    d = _eval(price=_price(fetched=closed - timedelta(seconds=30)), now=closed)
    assert d.readiness is Readiness.TRIGGER_PENDING and d.recommendation is Recommendation.CONDITIONAL_BUY  # type: ignore[attr-defined]


def test_failed_news_read_is_not_no_news() -> None:
    d = _eval(price=_price(bench=0.01), news=_news(CheckState.FAILED))   # relative +0.01 % < 0.5 %
    assert d.recommendation is Recommendation.WATCH  # type: ignore[attr-defined]
    assert any("kontrol edilemedi" in b for b in d.blockers)  # type: ignore[attr-defined]
    assert not any("başlık yok" in b for b in d.blockers)  # type: ignore[attr-defined]


def test_old_headline_does_not_count_as_new() -> None:
    d = _eval(price=_price(bench=0.01), news=_news(age_hours=48))
    assert d.recommendation is Recommendation.WATCH  # type: ignore[attr-defined]


def test_future_dated_headline_is_ignored() -> None:
    d = _eval(price=_price(bench=0.01), news=_news(age_hours=-1))
    assert d.recommendation is Recommendation.WATCH  # type: ignore[attr-defined]


def test_missing_price_data_is_watch_with_reason() -> None:
    price = replace(_price(), atr=None, state=CheckState.FAILED, blocker="ATR yok")
    d = _eval(price=price)
    assert d.recommendation is Recommendation.WATCH and "ATR yok" in d.blockers[0]  # type: ignore[attr-defined]


def test_card_carries_the_plan_numbers_and_the_research_counter_argument() -> None:
    d = _eval()
    views = (price_view("long_call", [(_q("110", "0.55", "0.60"), True)], LIVE_NOW, date(2026, 9, 24),
                        S.option_plan, COSTS, 100),)
    card = build_card(d, views, "", S)  # type: ignore[arg-type]
    assert card.recommendation is Recommendation.BUY
    assert "$101.00" in card.headline and "$97.00" in card.headline
    assert "H01/H02" in card.counter_argument
    assert card.evidence_status.value == "EXPERIMENTAL_RULES"
    assert card.opportunity_id == "2026-09-24:NVDA:up"
    assert "BusinessWire" in card.news_evidence and "URL" not in card.news_evidence
    payload = to_jsonable(card)
    assert payload["recommendation"] == "BUY" and payload["stock_plan"]["stop"] == 97.0


def test_bearish_card_without_an_executable_put_is_not_ready() -> None:
    prints = [_print(i, option_type="put") for i in range(1, 4)]
    d = _eval(price=_price(spot=97.0), flow=summarise("NVDA", "r", prints, S.flow))
    pending = price_view("long_put", [(_q("97", None, None, right="put"), True)], LIVE_NOW,
                         date(2026, 9, 24), S.option_plan, COSTS, 100)
    card = build_card(d, (pending,), "", S)  # type: ignore[arg-type]
    assert card.readiness is not Readiness.READY


def test_buy_with_no_executable_instrument_is_not_ready() -> None:
    # spot 3000: one share is above the 2500 PAPER notional cap -> 0 shares; no option either
    price = PriceContext(
        ticker="NVDA", spot=3000.0, spot_fetched_at=LIVE_NOW - timedelta(seconds=60), prev_close=2990.0,
        prev_close_day=date(2026, 9, 23), atr=20.0, atr_sessions=30, benchmark_move=0.0,
        state=CheckState.CHECKED_FOUND,
    )
    d = _eval(price=price)
    assert d.recommendation is Recommendation.BUY and d.readiness is Readiness.READY  # type: ignore[attr-defined]
    card = build_card(d, (), "yok", S)  # type: ignore[arg-type]
    assert card.readiness is not Readiness.READY
    assert card.instrument.preferred == "none"
    assert any("uygulanabilir araç yok" in b for b in card.blockers)
    assert "none" not in card.headline


def test_not_current_flow_is_never_ready() -> None:
    d = evaluate(_flow(), _price(), _news(), classify(LIVE_NOW, S.calendar), LIVE_NOW, S,  # type: ignore[arg-type]
                 flow_current=False)
    assert d.readiness is Readiness.TRIGGER_PENDING and d.recommendation is Recommendation.CONDITIONAL_BUY


def test_benchmark_card_does_not_compare_spy_with_itself() -> None:
    spy = replace(_price(), ticker="SPY")
    flow = summarise("SPY", "r", [replace(p, ticker="SPY") for p in _qualifying_up()], S.flow)
    d = evaluate(flow, spy, _news(CheckState.CHECKED_NONE), classify(LIVE_NOW, S.calendar), LIVE_NOW, S)
    card = build_card(d, (), "", S)
    assert "karşılaştırma ölçütünün kendisi" in card.price_evidence
    assert "SPY'a göre" not in card.price_evidence
