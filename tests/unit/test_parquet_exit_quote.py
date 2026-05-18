"""Phase 3.5.5 tests for ``ParquetExitQuoteProvider``.

Covers:
  - walking-back lookup: latest bid at-or-before the exit instant
  - exact-month hit
  - walk-back across months when the exit-month has no earlier row
  - missing contract → None
  - missing month file → None
  - exit before any data → None
  - Protocol conformance with ``ExitQuoteProvider``
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from uoa_detector.backtest import ParquetExitQuoteProvider
from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.backtest.simple_pnl import ExitQuoteProvider
from uoa_detector.domain.raw_print import RawPrint


def _print(
    *,
    ts: datetime,
    strike: Decimal,
    bid: Decimal,
    option_type: str = "call",
    expiry: date = date(2025, 8, 15),
    ticker: str = "AMD",
) -> RawPrint:
    return RawPrint(
        source_id="thetadata",
        source_event_id=f"e-{ts.isoformat()}-{strike}",
        timestamp=ts,
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike,
        expiry=expiry,
        dte=(expiry - ts.date()).days,
        spot_price=Decimal("0"),
        premium_paid=Decimal("1000"),
        option_price=bid + Decimal("0.10"),
        bid=bid,
        ask=bid + Decimal("0.20"),
        fill_side="above_ask",
        exchange="CBOE",
        implied_volatility=None,
        open_interest=None,
        is_iso=False,
        source_tags=(),
    )


def _write(dir_: Path, ticker: str, ym: str, prints: list[RawPrint]) -> None:
    prints = sorted(prints, key=lambda p: p.timestamp)
    write_parquet(prints, dir_ / ticker / f"{ym}.parquet")


def test_is_exit_quote_provider(tmp_path: Path) -> None:
    provider = ParquetExitQuoteProvider(tmp_path)
    assert isinstance(provider, ExitQuoteProvider)


def test_latest_bid_at_or_before_within_month(tmp_path: Path) -> None:
    k100 = Decimal("100")
    _write(
        tmp_path, "AMD", "2025-07",
        [
            _print(ts=datetime(2025, 7, 7, 14, tzinfo=UTC), strike=k100, bid=Decimal("2.00")),
            _print(ts=datetime(2025, 7, 9, 15, tzinfo=UTC), strike=k100, bid=Decimal("2.50")),
            _print(ts=datetime(2025, 7, 11, 16, tzinfo=UTC), strike=k100, bid=Decimal("3.00")),
        ],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=k100,
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 10, tzinfo=UTC),
    )
    # Latest at-or-before 2025-07-10 is the 07-09 print.
    assert bid == Decimal("2.50")


def test_walk_back_to_previous_month(tmp_path: Path) -> None:
    k100 = Decimal("100")
    _write(
        tmp_path, "AMD", "2025-06",
        [_print(ts=datetime(2025, 6, 20, 14, tzinfo=UTC), strike=k100, bid=Decimal("1.75"))],
    )
    # July file exists but has no row for this contract before the exit.
    _write(
        tmp_path, "AMD", "2025-07",
        [_print(ts=datetime(2025, 7, 25, 14, tzinfo=UTC), strike=k100, bid=Decimal("4.00"))],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=k100,
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 10, tzinfo=UTC),
    )
    # Nothing in July at-or-before the 10th → walk back to June's last bid.
    assert bid == Decimal("1.75")


def test_missing_contract_returns_none(tmp_path: Path) -> None:
    _write(
        tmp_path, "AMD", "2025-07",
        [_print(ts=datetime(2025, 7, 9, tzinfo=UTC), strike=Decimal("100"), bid=Decimal("2.50"))],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=Decimal("999"),
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 20, tzinfo=UTC),
    )
    assert bid is None


def test_missing_month_file_returns_none(tmp_path: Path) -> None:
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=Decimal("100"),
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 20, tzinfo=UTC),
    )
    assert bid is None


def test_exit_before_any_data_returns_none(tmp_path: Path) -> None:
    k100 = Decimal("100")
    _write(
        tmp_path, "AMD", "2025-07",
        [_print(ts=datetime(2025, 7, 25, tzinfo=UTC), strike=k100, bid=Decimal("2.50"))],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=k100,
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 10, tzinfo=UTC),
    )
    # The only July row is after the exit instant and there is no
    # earlier month → no quote.
    assert bid is None


def test_option_type_and_strike_disambiguate(tmp_path: Path) -> None:
    _write(
        tmp_path, "AMD", "2025-07",
        [
            _print(ts=datetime(2025, 7, 9, tzinfo=UTC), strike=Decimal("100"), bid=Decimal("2.50"), option_type="call"),
            _print(ts=datetime(2025, 7, 9, tzinfo=UTC), strike=Decimal("100"), bid=Decimal("5.50"), option_type="put"),
        ],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    at = datetime(2025, 7, 20, tzinfo=UTC)
    expiry = datetime(2025, 8, 15, tzinfo=UTC)
    call_bid = provider.get_bid(
        ticker="AMD", strike=Decimal("100"), expiry=expiry,
        option_type="call", at=at,
    )
    put_bid = provider.get_bid(
        ticker="AMD", strike=Decimal("100"), expiry=expiry,
        option_type="put", at=at,
    )
    assert call_bid == Decimal("2.50")
    assert put_bid == Decimal("5.50")
