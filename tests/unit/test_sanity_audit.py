"""Phase 3.5.7 — sanity-audit tooling tests.

Pins the four audit checks (look-ahead leakage, trade frequency, PnL
distribution, spread/slippage) so they are ready to run on the verdict
run the moment it produces trades.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from uoa_detector.backtest.pnl_provider import RealizedTrade
from uoa_detector.backtest.sanity_audit import (
    assess_trade_frequency,
    check_lookahead,
    pnl_distribution,
    spread_sanity,
)

_BASE = datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


def _closed(event_id: str, *, r: float, hold_minutes: int = 60) -> RealizedTrade:
    return RealizedTrade(
        event_id=event_id,
        realized_r=r,
        entry_ts=_BASE,
        exit_ts=_BASE + timedelta(minutes=hold_minutes),
        exit_reason="fixed_window_elapsed",
    )


def _open(event_id: str) -> RealizedTrade:
    return RealizedTrade(
        event_id=event_id,
        realized_r=None,
        entry_ts=_BASE,
        exit_ts=None,
        exit_reason="holding_window_open",
    )


# ---------------------------------------------------------------------------
# Look-ahead leakage
# ---------------------------------------------------------------------------


def test_lookahead_clean_closed_trades_ok() -> None:
    trades = [_closed("a", r=1.0), _closed("b", r=-0.5), _open("c")]
    rep = check_lookahead(trades)
    assert rep.ok
    assert rep.violations == ()
    assert rep.closed_trades == 2  # the open trade is not counted


def test_lookahead_flags_exit_not_after_entry() -> None:
    same_ts = RealizedTrade(
        event_id="same",
        realized_r=0.3,
        entry_ts=_BASE,
        exit_ts=_BASE,  # exit == entry → time-travel
        exit_reason="fixed_window_elapsed",
    )
    backwards = RealizedTrade(
        event_id="back",
        realized_r=0.3,
        entry_ts=_BASE,
        exit_ts=_BASE - timedelta(minutes=1),  # exit before entry
        exit_reason="fixed_window_elapsed",
    )
    rep = check_lookahead([_closed("good", r=1.0), same_ts, backwards])
    assert not rep.ok
    assert set(rep.violations) == {"same", "back"}


# ---------------------------------------------------------------------------
# The quote behind the exit (2026-09-20). Before this, check_lookahead compared
# the trade's own timestamps and never the quote's, which is how an exit priced
# off a bid from 18 days earlier passed every check a run made.
# ---------------------------------------------------------------------------


def _priced(event_id: str, *, quote_ts: datetime, exit_ts: datetime) -> RealizedTrade:
    """A closed trade that reports which quote priced its exit."""
    return RealizedTrade(
        event_id=event_id,
        realized_r=0.5,
        entry_ts=_BASE,
        exit_ts=exit_ts,
        exit_reason="fixed_window_elapsed",
        exit_quote_ts=quote_ts,
    )


def test_a_quote_from_the_exit_session_inside_the_position_passes() -> None:
    exit_ts = _BASE + timedelta(days=3)
    clean = _priced("clean", quote_ts=exit_ts - timedelta(minutes=5), exit_ts=exit_ts)
    rep = check_lookahead([clean])
    assert rep.ok
    assert rep.unverifiable == ()
    assert rep.fully_verified


def test_a_quote_recorded_before_the_position_opened_is_a_violation() -> None:
    """The leak the 2026-09-19 audit found, reduced to its arithmetic.

    ``parquet_exit_quote`` walked back up to three calendar months, so an exit on
    day 3 could be priced off a bid from 18 days BEFORE entry. Every timestamp the
    trade carried was correct; only the quote's was wrong, and nothing looked at it.
    """
    exit_ts = _BASE + timedelta(days=3)
    leaked = _priced("leak", quote_ts=_BASE - timedelta(days=18), exit_ts=exit_ts)
    rep = check_lookahead([leaked])
    assert not rep.ok
    assert rep.violations == ("leak",)


def test_a_quote_from_an_earlier_session_is_a_violation_even_inside_the_position() -> None:
    """Stale, not early: the quote sits between entry and exit but on another day.

    The option did not quote on the exit day, so the bid is the previous session's
    price. ``simple_pnl``'s parquet provider has refused this since 3.5.0.1; the
    audit can now confirm the refusal held instead of assuming it.
    """
    exit_ts = _BASE + timedelta(days=3)
    stale = _priced("stale", quote_ts=_BASE + timedelta(days=2), exit_ts=exit_ts)
    rep = check_lookahead([stale])
    assert not rep.ok
    assert rep.violations == ("stale",)


def test_a_quote_after_the_exit_is_a_violation() -> None:
    exit_ts = _BASE + timedelta(days=3)
    future = _priced("future", quote_ts=exit_ts + timedelta(minutes=1), exit_ts=exit_ts)
    rep = check_lookahead([future])
    assert not rep.ok
    assert rep.violations == ("future",)


def test_a_trade_without_a_quote_timestamp_is_unverifiable_not_clean() -> None:
    """Counting it clean would restore exactly the blindness this change removes.

    ``ok`` stays True because the trade breaks no rule that can be checked — but
    ``fully_verified`` is False and the event_id is named, so a run cannot report a
    green look-ahead check while half its trades were unexaminable.
    """
    rep = check_lookahead([_closed("blind", r=1.0)])
    assert rep.ok
    assert not rep.fully_verified
    assert rep.unverifiable == ("blind",)
    assert rep.violations == ()


def test_violations_and_blind_spots_are_counted_apart() -> None:
    exit_ts = _BASE + timedelta(days=3)
    rep = check_lookahead([
        _priced("good", quote_ts=exit_ts - timedelta(minutes=1), exit_ts=exit_ts),
        _priced("leak", quote_ts=_BASE - timedelta(days=18), exit_ts=exit_ts),
        _closed("blind", r=1.0),
        _open("still-open"),
    ])
    assert rep.closed_trades == 3          # the open trade is not counted
    assert rep.violations == ("leak",)
    assert rep.unverifiable == ("blind",)
    assert not rep.ok
    assert not rep.fully_verified


def test_the_pricing_path_asks_for_the_quote_and_not_the_bare_bid() -> None:
    """The guard that keeps the timestamp reaching the audit at all.

    ``get_bid`` still exists — 26 provider tests read it — so nothing stops a future
    edit from pricing an exit with it again and silently dropping the timestamp, which
    would make every trade 'unverifiable' and the audit blind once more. This reads
    the pricing module and asserts the production path does not.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src" / "uoa_detector" / "backtest" / "simple_pnl.py"
    ).read_text(encoding="utf-8")
    assert "self._quotes.get_quote(" in source, "the pricing path must ask for the quote"
    assert "self._quotes.get_bid(" not in source, (
        "the pricing path must not price an exit off a bare bid: the quote's timestamp "
        "would never reach RealizedTrade and check_lookahead would go blind again"
    )


# ---------------------------------------------------------------------------
# Trade frequency
# ---------------------------------------------------------------------------


def test_frequency_too_few() -> None:
    rep = assess_trade_frequency(19)
    assert rep.too_few and not rep.too_many
    assert "too_few" in rep.verdict


def test_frequency_too_many() -> None:
    rep = assess_trade_frequency(201)
    assert rep.too_many and not rep.too_few
    assert "too_many" in rep.verdict


def test_frequency_in_band_ok() -> None:
    for n in (20, 60, 200):
        rep = assess_trade_frequency(n)
        assert not rep.too_few and not rep.too_many
        assert rep.verdict == "ok"
        assert rep.target == 60


# ---------------------------------------------------------------------------
# PnL distribution
# ---------------------------------------------------------------------------


def test_distribution_empty() -> None:
    rep = pnl_distribution([_open("x")])
    assert rep.n == 0
    assert rep.mean_r is None and rep.win_rate is None


def test_distribution_stats() -> None:
    trades = [_closed("a", r=2.0), _closed("b", r=-1.0), _closed("c", r=1.0)]
    rep = pnl_distribution(trades)
    assert rep.n == 3
    assert rep.mean_r == 2.0 / 3
    assert rep.median_r == 1.0
    assert rep.min_r == -1.0
    assert rep.max_r == 2.0
    assert rep.win_rate == 2 / 3


# ---------------------------------------------------------------------------
# Spread (slippage) sanity
# ---------------------------------------------------------------------------


def test_spread_sanity_empty() -> None:
    rep = spread_sanity([], slippage_assumption_pct=0.02)
    assert rep.n == 0
    assert rep.median_pct is None
    assert not rep.optimistic


def test_spread_sanity_stats_and_optimistic_flag() -> None:
    # Median spread (0.03) exceeds the 0.02 assumption → optimistic flag.
    spreads = [0.01, 0.02, 0.03, 0.04, 0.10]
    rep = spread_sanity(spreads, slippage_assumption_pct=0.02)
    assert rep.n == 5
    assert rep.median_pct == 0.03
    assert rep.frac_over_assumption == 3 / 5  # 0.03, 0.04, 0.10
    assert rep.optimistic


def test_spread_sanity_not_optimistic_when_median_below_assumption() -> None:
    rep = spread_sanity([0.005, 0.01, 0.012], slippage_assumption_pct=0.02)
    assert not rep.optimistic
