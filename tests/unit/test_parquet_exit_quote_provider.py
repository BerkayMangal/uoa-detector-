"""Phase 3.5.0.1 tests for ``ParquetExitQuoteProvider``.

The provider reads option ``bid`` quotes from the ThetaData v3 replay
parquet layout (``{data_dir}/{ticker}/{YYYY-MM}.parquet``) and answers
``get_bid`` with the same walking-back semantics as
``DictExitQuoteProvider``, plus a same-trading-day staleness tolerance
(pinned decision #2 in ``docs/phase-3.5.0-acceptance.md``).

Covers:
  - happy path: exact-timestamp lookup returns the row's bid
  - walking-back: a later same-day ``at`` returns the last quote
  - staleness: an ``at`` on a later day than the newest quote → None
  - before-first-quote ``at`` → None
  - unknown contract (strike / expiry / option_type) → None
  - unknown ticker → None
  - ticker filter excludes an otherwise-present ticker → None
  - index is built once and cached
  - end-to-end with ``SimplePnLProvider``: a signal whose exit lands on
    a day with a real quote closes; a signal whose exit lands off-data
    stays open (never fabricated)

The committed synthetic fixture is AAPL-only, one quote per contract
(10 distinct strikes across 5 trading days, all expiry 2025-07-18).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.backtest import (
    BacktestStore,
    ParquetExitQuoteProvider,
    SimplePnLProvider,
)
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import BacktestConfig
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_FIXTURE_DIR = Path("tests/fixtures/historical/synthetic")

# The AAPL fixture's first row: 2025-06-09 14:35 UTC, call, strike 200,
# expiry 2025-07-18, bid 1.45.
_EXPIRY = datetime(2025, 7, 18, tzinfo=UTC)
_EXPIRY_D = date(2025, 7, 18)


def _provider(**kwargs: object) -> ParquetExitQuoteProvider:
    return ParquetExitQuoteProvider(_FIXTURE_DIR, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_bid — walking-back + staleness
# ---------------------------------------------------------------------------


def test_exact_timestamp_returns_row_bid() -> None:
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid == Decimal("1.45")


def test_walking_back_same_day_returns_last_quote() -> None:
    """An ``at`` later the same trading day still resolves the 14:35 quote."""
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 21, 0, tzinfo=UTC),
    )
    assert bid == Decimal("1.45")


def test_stale_quote_previous_day_returns_none() -> None:
    """The newest quote (06-09) is stale for an exit on 06-10 → None."""
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 10, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_before_first_quote_returns_none() -> None:
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 10, 0, tzinfo=UTC),
    )
    assert bid is None


def test_unknown_strike_returns_none() -> None:
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("999"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_unknown_expiry_returns_none() -> None:
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_wrong_option_type_returns_none() -> None:
    """Strike 200 is a call in the fixture; asking for the put → None."""
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="put",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_unknown_ticker_returns_none() -> None:
    p = _provider()
    bid = p.get_bid(
        ticker="MSFT",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_ticker_filter_excludes_present_ticker() -> None:
    p = _provider(tickers=["MSFT"])
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


def test_put_contract_resolves() -> None:
    """The fixture's row 1 is a put strike 201 @ 2025-06-09 20:35, bid 1.55."""
    p = _provider()
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("201"),
        expiry=_EXPIRY,
        option_type="put",
        at=datetime(2025, 6, 9, 20, 35, tzinfo=UTC),
    )
    assert bid == Decimal("1.55")


def test_index_built_once_and_cached() -> None:
    p = _provider()
    p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    # Second lookup reuses the cached index (same object identity).
    first = p._index["AAPL"]
    p.get_bid(
        ticker="AAPL",
        strike=Decimal("202"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 10, 14, 35, tzinfo=UTC),
    )
    assert p._index["AAPL"] is first


def test_missing_data_dir_returns_none(tmp_path: Path) -> None:
    p = ParquetExitQuoteProvider(tmp_path / "does-not-exist")
    bid = p.get_bid(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=_EXPIRY,
        option_type="call",
        at=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
    )
    assert bid is None


# ---------------------------------------------------------------------------
# End-to-end with SimplePnLProvider
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    return load_default_profile().backtest


def _signal(
    *,
    entry_ts: datetime,
    strike: Decimal,
    option_type: str,
    expiry_d: date,
    option_price: Decimal,
    max_r: float,
):
    store = BacktestStore()
    event = EnrichedEvent(
        print=OptionsPrint(
            event_id="e1",
            timestamp=entry_ts,
            ticker="AAPL",
            option_type=option_type,  # type: ignore[arg-type]
            strike=strike,
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
    decision = LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t")
    size = PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=max_r, scale_in=False)
    store.add(event, decision, size)
    return next(iter(store.iter_records("implicit-default")))


def test_simple_pnl_closes_when_exit_lands_on_a_quoted_day() -> None:
    """Entry 06-04, +5d window → exit 06-09, where strike-200 call quotes.

    floor = expiry(07-18) - 2 = 07-16, so the 5-day window binds first.
    The exit bid resolves from the fixture (1.45), so the trade closes.
    """
    signal = _signal(
        entry_ts=datetime(2025, 6, 4, 14, 35, tzinfo=UTC),
        strike=Decimal("200"),
        option_type="call",
        expiry_d=_EXPIRY_D,
        option_price=Decimal("1.20"),
        max_r=1.0,
    )
    pnl = SimplePnLProvider(_config(), _provider())
    trade = pnl.provide(signal)

    assert trade.realized_r is not None
    assert trade.exit_ts == datetime(2025, 6, 9, 14, 35, tzinfo=UTC)
    assert trade.exit_reason == "fixed_window_elapsed"
    # entry_ask = option_price = 1.20; slippage = 1.20 * 0.02 = 0.024.
    # pnl = (1.45 - 1.20) - 0.024 = 0.226; R = 0.226 / 1.20 * 1.0.
    expected_r = float((Decimal("0.25") - Decimal("0.024")) / Decimal("1.20"))
    assert trade.realized_r == pytest.approx(expected_r)


def test_simple_pnl_open_when_exit_off_data() -> None:
    """Entry 06-09, +5d window → exit 06-14 (weekend, no quote) → open."""
    signal = _signal(
        entry_ts=datetime(2025, 6, 9, 14, 35, tzinfo=UTC),
        strike=Decimal("200"),
        option_type="call",
        expiry_d=_EXPIRY_D,
        option_price=Decimal("1.20"),
        max_r=1.0,
    )
    pnl = SimplePnLProvider(_config(), _provider())
    trade = pnl.provide(signal)

    assert trade.realized_r is None
    assert trade.exit_reason == "holding_window_open"
