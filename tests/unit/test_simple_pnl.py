"""Phase 3.2.3.3 tests for ``SimplePnLProvider``.

Covers:
  - PnL formula (entry=ask, exit=bid, slippage haircut, R-units)
  - fixed_window strategy: exit at +N days, capped by DTE floor
  - dte_based strategy: exit at DTE threshold, capped by floor
  - take_profit_or_stop: raises NotImplementedError with Phase-3.4 hint
  - max_r=0 (no position) → open trade, no realized R
  - DictExitQuoteProvider walking-back lookup
  - missing exit quote → open trade
  - degenerate entry price → open trade
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.backtest import (
    BacktestStore,
    DictExitQuoteProvider,
    SimplePnLProvider,
)
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import BacktestConfig
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_signal(
    *,
    event_id: str = "e1",
    entry_ts: datetime | None = None,
    expiry_d: date | None = None,
    option_price: Decimal = Decimal("1.50"),
    max_r: float = 1.0,
):
    if entry_ts is None:
        entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    if expiry_d is None:
        expiry_d = date(2025, 7, 18)
    store = BacktestStore()
    event = EnrichedEvent(
        print=OptionsPrint(
            event_id=event_id,
            timestamp=entry_ts,
            ticker="AAPL",
            option_type="call",
            strike=Decimal("200"),
            expiry=expiry_d,
            dte=(expiry_d - entry_ts.date()).days,
            spot_price=Decimal("198"),
            premium_paid=Decimal("1000"),
            option_price=option_price,
            implied_volatility=0.45,
            bid=option_price - Decimal("0.05"),
            ask=option_price + Decimal("0.05"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=False,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )
    decision = LabelDecision(
        label=(
            SignalLabel.STANDARD_UOA if max_r > 0 else SignalLabel.IGNORE_NOISE
        ),
        reason="t",
    )
    size = PositionSize(
        bucket=(
            RiskBucket.STANDARD_UOA if max_r > 0 else RiskBucket.DISCARD
        ),
        max_r=max_r,
        scale_in=False,
    )
    store.add(event, decision, size)
    return next(iter(store.iter_records("implicit-default")))


def _config(
    *,
    holding_strategy: str = "fixed_window",
    holding_window_days: int = 5,
    exit_on_dte_lte: int = 2,
    dte_based_close_threshold: int = 7,
    slippage_pct: float = 0.02,
) -> BacktestConfig:
    base = load_default_profile().backtest
    return base.model_copy(
        update={
            "holding_strategy": holding_strategy,
            "holding_window_days": holding_window_days,
            "exit_on_dte_lte": exit_on_dte_lte,
            "dte_based_close_threshold": dte_based_close_threshold,
            "slippage_pct": slippage_pct,
        },
    )


def _quotes_with_one_exit(
    *,
    exit_ts: datetime,
    bid: Decimal,
    ticker: str = "AAPL",
    strike: Decimal = Decimal("200"),
    expiry_d: date | None = None,
    option_type: str = "call",
) -> DictExitQuoteProvider:
    if expiry_d is None:
        expiry_d = date(2025, 7, 18)
    expiry_iso = expiry_d.isoformat()
    return DictExitQuoteProvider(
        {(ticker, option_type, strike, expiry_iso): [(exit_ts, bid)]},
    )


# ---------------------------------------------------------------------------
# Formula correctness
# ---------------------------------------------------------------------------


def test_simple_pnl_winner_returns_positive_r() -> None:
    """Entry ask=1.50, exit bid=2.50, slippage 2% of 1.50 = 0.03.
    Per-contract PnL = 1.00 - 0.03 = 0.97. R = 0.97 / 1.50 = 0.6467.
    With max_r=1.0, realized_r = 0.6467.
    """
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    sig = _build_signal(
        entry_ts=entry_ts, option_price=Decimal("1.50"), max_r=1.0,
    )
    expected_exit = entry_ts + timedelta(days=5)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("2.50"),
    )
    provider = SimplePnLProvider(_config(), quotes)

    out = provider.provide(sig)
    assert out.realized_r is not None
    assert out.realized_r == pytest.approx(0.6467, abs=1e-3)
    assert out.exit_reason == "fixed_window_elapsed"
    assert out.exit_ts == expected_exit


def test_simple_pnl_loser_returns_negative_r() -> None:
    """Entry ask=2.00, exit bid=1.00, slippage 2% of 2.00 = 0.04.
    PnL = -1.00 - 0.04 = -1.04. R = -1.04 / 2.00 = -0.52.
    """
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    sig = _build_signal(
        entry_ts=entry_ts, option_price=Decimal("2.00"), max_r=1.0,
    )
    expected_exit = entry_ts + timedelta(days=5)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("1.00"),
    )
    provider = SimplePnLProvider(_config(), quotes)

    out = provider.provide(sig)
    assert out.realized_r is not None
    assert out.realized_r == pytest.approx(-0.52, abs=1e-6)


def test_simple_pnl_max_r_scales_realized_r() -> None:
    """With max_r=0.5, realized_r is half of the max_r=1.0 case."""
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    sig_full = _build_signal(
        entry_ts=entry_ts, option_price=Decimal("1.50"), max_r=1.0,
        event_id="full",
    )
    sig_half = _build_signal(
        entry_ts=entry_ts, option_price=Decimal("1.50"), max_r=0.5,
        event_id="half",
    )
    expected_exit = entry_ts + timedelta(days=5)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("2.50"),
    )
    provider = SimplePnLProvider(_config(), quotes)

    full_r = provider.provide(sig_full).realized_r
    half_r = provider.provide(sig_half).realized_r
    assert full_r is not None
    assert half_r is not None
    assert half_r == pytest.approx(full_r / 2, abs=1e-6)


def test_simple_pnl_slippage_zero_matches_no_haircut() -> None:
    """slippage_pct=0 → realized_r == raw (exit-entry)/entry."""
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    sig = _build_signal(
        entry_ts=entry_ts, option_price=Decimal("1.00"), max_r=1.0,
    )
    expected_exit = entry_ts + timedelta(days=5)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("1.50"),
    )
    provider = SimplePnLProvider(_config(slippage_pct=0.0), quotes)
    out = provider.provide(sig)
    assert out.realized_r == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# fixed_window strategy
# ---------------------------------------------------------------------------


def test_fixed_window_exit_capped_by_dte_floor() -> None:
    """Holding window 5 days, but expiry is in 4 days with floor=2 →
    forced exit at expiry-2 = 2 days from entry, NOT 5.
    """
    entry_ts = datetime(2025, 7, 14, 15, 30, tzinfo=UTC)
    expiry_d = date(2025, 7, 18)  # 4 days away
    sig = _build_signal(
        entry_ts=entry_ts, expiry_d=expiry_d, option_price=Decimal("1.00"),
    )
    floor_exit = datetime.combine(expiry_d, datetime.min.time(), tzinfo=UTC) - timedelta(days=2)
    quotes = _quotes_with_one_exit(
        exit_ts=floor_exit, bid=Decimal("1.20"), expiry_d=expiry_d,
    )
    provider = SimplePnLProvider(_config(), quotes)
    out = provider.provide(sig)
    assert out.exit_reason == "exit_on_dte_lte_floor"
    assert out.exit_ts == floor_exit


def test_signal_inside_dte_floor_stays_open_no_lookahead() -> None:
    """Phase 3.6 leak fix: a signal whose DTE is already at/inside the
    exit_on_dte_lte floor has its scheduled exit at or before entry.

    Realizing it would price the exit off a PRE-ENTRY quote (a negative
    holding period — look-ahead). It must stay open (realized_r None).
    """
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    # Expiry one day out; with exit_on_dte_lte=2 the floor exit lands at
    # expiry-2 = 2025-06-10, i.e. before entry.
    sig = _build_signal(
        entry_ts=entry_ts, expiry_d=date(2025, 6, 12), max_r=1.0,
    )
    # A juicy stale bid exists a day BEFORE entry — the bug would have
    # closed against it for a fabricated winner.
    quotes = _quotes_with_one_exit(
        exit_ts=entry_ts - timedelta(days=1),
        bid=Decimal("9.99"),
        expiry_d=date(2025, 6, 12),
    )
    out = SimplePnLProvider(_config(exit_on_dte_lte=2), quotes).provide(sig)
    assert out.realized_r is None
    assert out.exit_ts is None
    assert out.exit_reason == "holding_window_open"


def test_fixed_window_normal_exit_uses_window_days() -> None:
    """With expiry far away, the window decides — 5 days from entry."""
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    expiry_d = date(2025, 8, 15)  # 65 days away
    sig = _build_signal(
        entry_ts=entry_ts, expiry_d=expiry_d, option_price=Decimal("1.00"),
    )
    expected_exit = entry_ts + timedelta(days=5)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("1.20"), expiry_d=expiry_d,
    )
    provider = SimplePnLProvider(_config(), quotes)
    out = provider.provide(sig)
    assert out.exit_reason == "fixed_window_elapsed"
    assert out.exit_ts == expected_exit


# ---------------------------------------------------------------------------
# dte_based strategy
# ---------------------------------------------------------------------------


def test_dte_based_exit_uses_threshold() -> None:
    """Strategy=dte_based, threshold=7 → exit at expiry-7 days.
    With expiry 30 days away, that's 23 days from entry.
    """
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    expiry_d = date(2025, 7, 11)  # 30 days
    sig = _build_signal(
        entry_ts=entry_ts, expiry_d=expiry_d, option_price=Decimal("1.00"),
    )
    expected_exit = datetime.combine(expiry_d, datetime.min.time(), tzinfo=UTC) - timedelta(days=7)
    quotes = _quotes_with_one_exit(
        exit_ts=expected_exit, bid=Decimal("1.20"), expiry_d=expiry_d,
    )
    provider = SimplePnLProvider(
        _config(holding_strategy="dte_based", dte_based_close_threshold=7),
        quotes,
    )
    out = provider.provide(sig)
    assert out.exit_reason == "dte_threshold_reached"
    assert out.exit_ts == expected_exit


def test_dte_based_capped_by_floor() -> None:
    """When threshold is below floor, the floor wins.

    threshold=1, floor=2 → floor at expiry-2 is *earlier* than
    threshold at expiry-1 → floor wins.
    """
    entry_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    expiry_d = date(2025, 7, 11)
    sig = _build_signal(
        entry_ts=entry_ts, expiry_d=expiry_d, option_price=Decimal("1.00"),
    )
    floor_exit = datetime.combine(expiry_d, datetime.min.time(), tzinfo=UTC) - timedelta(days=2)
    quotes = _quotes_with_one_exit(
        exit_ts=floor_exit, bid=Decimal("1.20"), expiry_d=expiry_d,
    )
    provider = SimplePnLProvider(
        _config(
            holding_strategy="dte_based",
            dte_based_close_threshold=1,
            exit_on_dte_lte=2,
        ),
        quotes,
    )
    out = provider.provide(sig)
    assert out.exit_reason == "exit_on_dte_lte_floor"


# ---------------------------------------------------------------------------
# take_profit_or_stop — explicit Phase 3.4 placeholder
# ---------------------------------------------------------------------------


def test_take_profit_or_stop_raises_with_phase_3_4_message() -> None:
    """SimplePnLProvider raises NotImplementedError on this strategy.

    The message must mention 'Phase 3.4' so future-self knows where
    to look. Profiles that mistakenly select this strategy in 3.2.3
    fail at first call, not silently degrade.
    """
    sig = _build_signal()
    quotes = _quotes_with_one_exit(
        exit_ts=sig.timestamp + timedelta(days=5), bid=Decimal("2.00"),
    )
    provider = SimplePnLProvider(
        _config(holding_strategy="take_profit_or_stop"),
        quotes,
    )
    with pytest.raises(NotImplementedError, match=r"Phase 3\.4"):
        provider.provide(sig)


# ---------------------------------------------------------------------------
# Open / no-position cases
# ---------------------------------------------------------------------------


def test_max_r_zero_returns_open_trade() -> None:
    """A decision with max_r=0 (no position taken) → open trade, no R."""
    sig = _build_signal(max_r=0.0, event_id="zero-r")
    quotes = DictExitQuoteProvider({})
    provider = SimplePnLProvider(_config(), quotes)
    out = provider.provide(sig)
    assert out.realized_r is None
    assert out.exit_reason == "holding_window_open"


def test_missing_exit_quote_returns_open_trade() -> None:
    """No quote at the exit time → can't compute PnL → open trade."""
    sig = _build_signal()
    # Empty dict — no quotes for any option.
    quotes = DictExitQuoteProvider({})
    provider = SimplePnLProvider(_config(), quotes)
    out = provider.provide(sig)
    assert out.realized_r is None
    assert out.exit_reason == "holding_window_open"


def test_degenerate_entry_price_returns_open_trade() -> None:
    """Entry price <= 0 → can't compute R → open trade."""
    sig = _build_signal(option_price=Decimal("0"))
    quotes = _quotes_with_one_exit(
        exit_ts=sig.timestamp + timedelta(days=5),
        bid=Decimal("0.50"),
    )
    provider = SimplePnLProvider(_config(), quotes)
    out = provider.provide(sig)
    assert out.realized_r is None


# ---------------------------------------------------------------------------
# DictExitQuoteProvider — walking-back lookup
# ---------------------------------------------------------------------------


def test_dict_exit_quote_provider_walks_back() -> None:
    """get_bid returns the latest entry at-or-before the requested time."""
    expiry = datetime(2025, 7, 18, tzinfo=UTC)
    quotes = DictExitQuoteProvider({
        ("AAPL", "call", Decimal("200"), "2025-07-18"): [
            (datetime(2025, 6, 12, 12, 0, tzinfo=UTC), Decimal("1.0")),
            (datetime(2025, 6, 14, 12, 0, tzinfo=UTC), Decimal("1.5")),
            (datetime(2025, 6, 16, 12, 0, tzinfo=UTC), Decimal("2.0")),
        ],
    })

    # Exact match.
    bid = quotes.get_bid(
        ticker="AAPL", strike=Decimal("200"), expiry=expiry,
        option_type="call",
        at=datetime(2025, 6, 14, 12, 0, tzinfo=UTC),
    )
    assert bid == Decimal("1.5")

    # Between two points → walks back to the earlier one.
    bid = quotes.get_bid(
        ticker="AAPL", strike=Decimal("200"), expiry=expiry,
        option_type="call",
        at=datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
    )
    assert bid == Decimal("1.5")

    # Before any data point → None.
    bid = quotes.get_bid(
        ticker="AAPL", strike=Decimal("200"), expiry=expiry,
        option_type="call",
        at=datetime(2025, 6, 11, 12, 0, tzinfo=UTC),
    )
    assert bid is None


def test_dict_exit_quote_provider_rejects_unsorted_input() -> None:
    """Construction asserts the price path is sorted; out-of-order raises."""
    with pytest.raises(ValueError, match="not sorted"):
        DictExitQuoteProvider({
            ("AAPL", "call", Decimal("200"), "2025-07-18"): [
                (datetime(2025, 6, 14, 12, 0, tzinfo=UTC), Decimal("1.5")),
                (datetime(2025, 6, 12, 12, 0, tzinfo=UTC), Decimal("1.0")),
            ],
        })


def test_simple_pnl_is_deterministic() -> None:
    """Same input → same output. Reproducibility precondition for
    Phase 3.2.3.4 metric calculator's reproducibility test.
    """
    sig = _build_signal()
    quotes = _quotes_with_one_exit(
        exit_ts=sig.timestamp + timedelta(days=5), bid=Decimal("2.00"),
    )
    provider = SimplePnLProvider(_config(), quotes)
    a = provider.provide(sig)
    b = provider.provide(sig)
    assert a == b
