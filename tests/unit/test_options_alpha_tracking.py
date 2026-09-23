"""Open PAPER position tracking (Phase 5.24): calendar, marks, exit decision."""

from __future__ import annotations

import pathlib
from datetime import date
from decimal import Decimal

import pytest

from uoa_detector.options_alpha.exits import ExitReason, ExitVariantId
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings
from uoa_detector.options_alpha.tracking import (
    LegQuote,
    mark_session,
    parse_historic_quotes,
    position_from_card,
    sessions_after,
    track,
)

LONG, SHORT = "SPY261009C00775000", "SPY261009C00776000"
CARD = {
    "signal_id": "sig_test", "underlying": "SPY", "session": "2026-09-22",
    "net_debit_per_share": "0.58", "commission_usd": "5.20", "quantity": 2,
    "legs": [
        {"occ_symbol": LONG, "side": "long", "strike": "775", "expiry": "2026-10-09"},
        {"occ_symbol": SHORT, "side": "short", "strike": "776", "expiry": "2026-10-09"},
    ],
}
CAL = [date(2026, 9, d) for d in (22, 23, 24, 25, 28, 29, 30)]


@pytest.fixture(scope="module")
def settings() -> OptionsAlphaSettings:
    return load_settings(pathlib.Path("profiles/options_alpha_v1.yaml"))


def q(bid: str | None, ask: str | None) -> LegQuote:
    return LegQuote(bid=None if bid is None else Decimal(bid), ask=None if ask is None else Decimal(ask))


def quotes(values: dict[date, tuple[str | None, str | None]]) -> dict[str, dict[date, LegQuote]]:
    """values: day -> (long bid, short ask)."""
    out: dict[str, dict[date, LegQuote]] = {LONG: {}, SHORT: {}}
    for day, (long_bid, short_ask) in values.items():
        if long_bid is not None:
            out[LONG][day] = q(long_bid, None)
        if short_ask is not None:
            out[SHORT][day] = q(None, short_ask)
    return out


def test_card_commission_is_already_the_total_and_is_not_multiplied_again() -> None:
    """card.py writes price.commission_usd * structures; the first real run showed
    that multiplying by the quantity again double-charged a 2-structure card."""
    p = position_from_card(CARD)
    assert p.quantity == 2
    assert p.commission_usd == Decimal("5.20")      # the card's total, as written
    assert p.max_profit_per_share == Decimal("0.42")  # width 1 - debit 0.58
    assert p.expiry == date(2026, 10, 9)


def test_historic_payload_keeps_missing_sides_missing() -> None:
    payload = {"chains": [
        {"date": "2026-09-23", "nbbo_bid": "7.10", "nbbo_ask": "7.14", "volume": 12},
        {"date": "2026-09-24", "nbbo_bid": None, "nbbo_ask": "7.30", "volume": 0},
        {"date": "bad", "nbbo_bid": "1"},
    ]}
    out = parse_historic_quotes(payload)
    assert out[date(2026, 9, 23)] == LegQuote(Decimal("7.10"), Decimal("7.14"), 12)
    assert out[date(2026, 9, 24)].bid is None
    assert len(out) == 2


def test_sessions_come_from_the_calendar_strictly_after_entry() -> None:
    assert sessions_after(CAL, date(2026, 9, 22), date(2026, 9, 25)) == (
        date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25),
    )


def test_a_calendar_day_with_a_missing_leg_is_unpriced_not_skipped(
    settings: OptionsAlphaSettings,
) -> None:
    """score_card derived days from the long leg's history, so this day vanished."""
    p = position_from_card(CARD)
    days = sessions_after(CAL, p.entry_session, date(2026, 9, 29))
    qs = quotes({date(2026, 9, 23): ("0.60", None)} | {d: ("0.60", "0.05") for d in days[1:]})
    result = track(p, days, qs, settings)
    assert result.marks[0].closable_value is None
    assert result.marks[0].missing_legs == (SHORT,)
    assert result.outcomes[ExitVariantId.TIME_ONLY].unpriced_days == 1


def test_closable_value_is_leg_by_leg_and_may_be_negative() -> None:
    p = position_from_card(CARD)
    m = mark_session(p, date(2026, 9, 23), quotes({date(2026, 9, 23): ("0.05", "0.20")}))
    assert m.closable_value == Decimal("-0.15")


def test_mid_hold_the_position_is_still_open(settings: OptionsAlphaSettings) -> None:
    p = position_from_card(CARD)
    days = sessions_after(CAL, p.entry_session, date(2026, 9, 24))
    result = track(p, days, quotes({d: ("0.60", "0.02") for d in days}), settings)
    assert not result.resolved
    assert result.plan.reason is ExitReason.STILL_OPEN
    assert result.plan.pnl_usd is None


def test_a_full_horizon_closes_on_time_with_net_pnl(settings: OptionsAlphaSettings) -> None:
    p = position_from_card(CARD)
    days = sessions_after(CAL, p.entry_session, date(2026, 9, 29))
    assert len(days) == 5
    result = track(p, days, quotes({d: ("0.66", "0.02") for d in days}), settings)
    time_only = result.outcomes[ExitVariantId.TIME_ONLY]
    assert time_only.reason is ExitReason.TIME
    assert time_only.exit_value == Decimal("0.64")
    # (0.64 - 0.58) x 100 x 2 - 5.20
    assert time_only.pnl_usd == Decimal("6.80")


def test_the_stop_fires_on_the_package_value(settings: OptionsAlphaSettings) -> None:
    p = position_from_card(CARD)
    days = sessions_after(CAL, p.entry_session, date(2026, 9, 29))
    qs = quotes({days[0]: ("0.40", "0.15"), **{d: ("0.60", "0.02") for d in days[1:]}})
    result = track(p, days, qs, settings)
    assert result.resolved
    assert result.plan.reason is ExitReason.STOP
    assert result.plan.exit_day == days[0]


def test_the_tracker_reproduces_score_card_on_the_two_real_cards(settings: OptionsAlphaSettings) -> None:
    """Real UW quotes, recorded 2026-09-23: the tracker must land on the same net P&L
    as the committed score_card outcomes, for every variant, for both 2026-09-08 cards."""
    import json

    fixture = json.loads(pathlib.Path(
        "tests/fixtures/options_alpha/tracker_2026-09-08_historic.json").read_text(encoding="utf-8"))
    for underlying in ("SPY", "QQQ"):
        entry = fixture[underlying]
        card = json.loads(pathlib.Path(entry["card"]).read_text(encoding="utf-8"))["card"]
        expected = json.loads(pathlib.Path(entry["card"].replace(".json", "_outcome.json"))
                              .read_text(encoding="utf-8"))["exits"]
        position = position_from_card(card)
        quotes = {occ: parse_historic_quotes(payload) for occ, payload in entry["historic"].items()}
        sessions = tuple(date.fromisoformat(d) for d in entry["sessions"])
        result = track(position, sessions, quotes, settings)
        for variant in ExitVariantId:
            got = result.outcomes[variant].pnl_usd
            assert got is not None
            assert str(got) == expected[variant.value]["pnl_usd"], (underlying, variant)
