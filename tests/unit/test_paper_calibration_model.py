"""Point-in-time entry gates and risk accounting for stock PAPER arms."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from webapp.paper_calibration.model import Arm, Quote, Rules, Seed, State, Tick, advance, start

DAY = date(2026, 11, 19)
PUBLISHED = datetime(2026, 11, 19, 15, 31, 29, tzinfo=UTC)
RULES = Rules(risk_usd=100, max_notional_usd=2500, slippage_bps=5, max_quote_age_seconds=900)
LEGACY = Arm(key="legacy", label="Eski giriş", confirm_entry=False, stop_atr=1.5)
GUARDED = Arm(key="confirmed", label="AL teyidi", confirm_entry=True, stop_atr=1.5)
WIDER = Arm(key="confirmed_2atr", label="AL teyidi · 2 ATR", confirm_entry=True, stop_atr=2)
SEED = Seed(
    opportunity_id="2026-11-19:EXMPL:up", ticker="EXMPL", session=DAY, published_at=PUBLISHED,
    source_scan_id="first-scan", rec_id="first-rec", source_path="P1_news_continuation", headline="Örnek hisse AL",
    reference=154, stop=139, target=184, chase=161, atr=10, shares=6,
)


def observation(
    elapsed: int, price: float | None, *, entry_check: str = "ready", quote_elapsed: int | None = None,
    session: date = DAY, market_open: bool = True, opposite_flow: bool = False, horizon_reached: bool = False,
) -> Tick:
    observed = PUBLISHED + timedelta(seconds=elapsed)
    source = observed if quote_elapsed is None else PUBLISHED + timedelta(seconds=quote_elapsed)
    return Tick(at=observed, session=session, market_open=market_open,
                quote=Quote(at=source, price=price) if price is not None else None,
                entry_check=entry_check, opposite_flow=opposite_flow, horizon_reached=horizon_reached)


def test_watch_blocks_confirmed_arm_but_baseline_opens_and_frozen_plan_can_fill_later() -> None:
    watch = observation(396, 151.8, entry_check="watch", quote_elapsed=370)
    raw = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), watch)
    guarded = advance(SEED, GUARDED, RULES, start(SEED, GUARDED, RULES), watch)
    assert raw.position.state is State.OPEN
    assert raw.position.entry_fill == 151.8759
    assert raw.position.entry_quote == watch.quote
    assert guarded.position.state is State.PENDING
    assert guarded.reason == "entry_watch"
    ready = observation(500, 151.8, quote_elapsed=370)
    confirmed = advance(SEED, GUARDED, RULES, guarded.position, ready)
    assert confirmed.position.state is State.OPEN
    assert confirmed.position.entry_at == ready.at
    assert confirmed.position.entry_fill == raw.position.entry_fill
    assert confirmed.position.stop == raw.position.stop == 139


def test_wider_stop_uses_same_absolute_target_original_entry_range_and_whole_share_budget() -> None:
    wide = start(SEED, WIDER, RULES)
    assert wide.stop == 134
    assert wide.quantity == 5
    assert wide.planned_risk == pytest.approx(100)
    assert wide.target == SEED.target
    step = advance(SEED, WIDER, RULES, wide, observation(396, 137.0))
    assert step.reason == "entry_price_outside_plan"
    assert step.position.state is State.PENDING


def test_zero_share_arm_preserves_unfilled_reason() -> None:
    narrow_risk_budget = RULES.model_copy(update={"risk_usd": 4.0})
    wide = start(SEED, WIDER, narrow_risk_budget)
    assert wide.state is State.UNFILLED
    assert wide.exit_reason == "risk_blocked"
    assert wide.quantity == 0
    assert wide.net_r is None


@pytest.mark.parametrize(
    ("elapsed", "source_time", "market_open", "expected"),
    [(100, 0, True, "quote_prepublication"), (100, 120, True, "quote_unavailable"),
     (1000, 99, True, "quote_unavailable"), (1000, 100, True, "entry_confirmed"),
     (100, 100, False, "market_closed"), (0, 0, True, "nonforward_observation")],
)
def test_only_post_publication_fresh_quotes_in_the_same_session_can_fill(
    elapsed: int, source_time: int, market_open: bool, expected: str,
) -> None:
    tick = observation(elapsed, 151.0, quote_elapsed=source_time, market_open=market_open)
    step = advance(SEED, GUARDED, RULES, start(SEED, GUARDED, RULES), tick)
    assert step.reason == expected
    if expected == "entry_confirmed":
        assert step.position.state is State.OPEN
    else:
        assert step.position.state is State.PENDING


@pytest.mark.parametrize(("entry_check", "expected"), [("missing", "entry_missing"), ("other", "entry_other")])
def test_guard_rejects_missing_and_nonstock_current_cards(entry_check: str, expected: str) -> None:
    tick = observation(396, 151.8, entry_check=entry_check)
    step = advance(SEED, GUARDED, RULES, start(SEED, GUARDED, RULES), tick)
    assert step.reason == expected


def test_unfilled_expires_on_session_change() -> None:
    step = advance(SEED, GUARDED, RULES, start(SEED, GUARDED, RULES),
                   observation(86400, 150.0, session=date(2026, 11, 20)))
    assert step.position.state is State.UNFILLED
    assert step.position.entry_at is None
    assert step.position.pnl_usd is None


def test_stop_prices_first_crossing_quote_before_opposing_flow_and_horizon() -> None:
    entry = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), observation(396, 151.8)).position
    price_up = advance(SEED, LEGACY, RULES, entry, observation(720, 154.0)).position
    stop_tick = observation(86400, 138.9, session=date(2026, 11, 20), opposite_flow=True, horizon_reached=True)
    stopped = advance(SEED, LEGACY, RULES, price_up, stop_tick)
    assert stopped.position.state is State.CLOSED
    assert stopped.reason == "stop"
    assert stopped.position.exit_fill == round(138.9 * 0.9995, 4)
    assert stopped.position.exit_quote == stop_tick.quote
    assert stopped.position.pnl_usd == round((round(138.9 * 0.9995, 4) - 151.8759) * 6, 2)
    assert stopped.position.net_r == pytest.approx((round(138.9 * 0.9995, 4) - 151.8759) * 6 / 90, abs=1e-6)
    assert stopped.position.mae_r == stopped.position.net_r
    assert stopped.position.mfe_r == pytest.approx((153.923 - 151.8759) * 6 / 90, abs=1e-6)


def test_missing_exit_quote_does_not_use_price_sampled_before_the_exit_signal() -> None:
    entry = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), observation(396, 151.8)).position
    requested = advance(SEED, LEGACY, RULES, entry, observation(500, None, opposite_flow=True))
    assert requested.position.state is State.EXIT_PENDING
    assert requested.position.exit_signal_at == PUBLISHED + timedelta(seconds=500)
    late_receipt = observation(600, 156.0, quote_elapsed=450)
    unresolved = advance(SEED, LEGACY, RULES, requested.position, late_receipt)
    assert unresolved.position.state is State.EXIT_PENDING
    same_session = advance(SEED, LEGACY, RULES, unresolved.position, observation(610, 155.0))
    assert same_session.position.state is State.CLOSED
    assert same_session.reason == "opposite_flow"
    assert same_session.position.exit_at == PUBLISHED + timedelta(seconds=610)


def test_exit_missing_at_next_session_is_unknown_and_immutable() -> None:
    entry = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), observation(396, 151.8)).position
    signalled = advance(SEED, LEGACY, RULES, entry, observation(500, None, horizon_reached=True)).position
    next_session = observation(86400, 160.0, session=date(2026, 11, 20))
    step = advance(SEED, LEGACY, RULES, signalled, next_session)
    assert step.position.state is State.UNKNOWN
    assert step.position.exit_reason == "horizon"
    assert step.position.exit_at is None
    assert step.position.pnl_usd is None
    assert advance(SEED, LEGACY, RULES, step.position, next_session).action == "noop"


def test_open_position_does_not_go_back_in_quote_time() -> None:
    opened = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), observation(396, 151.8)).position
    old = advance(SEED, LEGACY, RULES, opened, observation(500, 139.0, quote_elapsed=394))
    corrected = advance(SEED, LEGACY, RULES, opened, observation(500, 139.0, quote_elapsed=396))
    assert old.position == corrected.position == opened
    assert old.reason == corrected.reason == "quote_unavailable"


def test_quote_from_previous_et_session_cannot_liquidate_a_position() -> None:
    opened = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), observation(396, 151.8)).position
    next_et_day = datetime(2026, 11, 20, 5, 2, tzinfo=UTC)
    tick = Tick(at=next_et_day, session=date(2026, 11, 20), market_open=True,
                quote=Quote(at=next_et_day - timedelta(minutes=5), price=130.0),
                entry_check="ready", opposite_flow=False, horizon_reached=False)
    step = advance(SEED, LEGACY, RULES, opened, tick)
    assert step.position == opened
    assert step.reason == "quote_unavailable"



def test_source_plan_cannot_exceed_risk_ceiling_by_rounding_both_levels() -> None:
    rounded = SEED.model_copy(update={"reference": 5.13, "stop": 4.87, "shares": 398})
    original = start(rounded, LEGACY, RULES)
    assert original.stop == 4.87
    assert original.quantity == 384
    assert original.planned_risk == pytest.approx(99.84)


def test_newer_source_mark_sampled_before_fill_cannot_trigger_stop() -> None:
    entry = observation(396, 151.8, quote_elapsed=370)
    opened = advance(SEED, LEGACY, RULES, start(SEED, LEGACY, RULES), entry).position
    before_fill = observation(450, 139.0, quote_elapsed=390)
    skipped = advance(SEED, LEGACY, RULES, opened, before_fill)
    assert skipped.position == opened
    assert skipped.reason == "quote_unavailable"
    after_fill = observation(500, 138.5, quote_elapsed=480)
    stopped = advance(SEED, LEGACY, RULES, opened, after_fill)
    assert stopped.position.state is State.CLOSED
    assert stopped.reason == "stop"
