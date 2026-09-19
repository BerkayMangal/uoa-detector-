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

from uoa_detector.backtest.parquet_exit_quote import ParquetExitQuoteProvider
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


def test_latest_bid_at_or_before_within_the_session(tmp_path: Path) -> None:
    """The latest quote at-or-before the exit, chosen from the exit's own session.

    The fixture used to span three days (07-07, 07-09, 07-11) against an exit on
    07-10, so it asserted that a bid from the day BEFORE could price the exit —
    the defect the 2026-09-19 audit found. Moved onto one session so it pins the
    at-or-before rule, which is what its name is about.
    """
    k100 = Decimal("100")
    _write(
        tmp_path, "AMD", "2025-07",
        [
            _print(ts=datetime(2025, 7, 10, 13, tzinfo=UTC), strike=k100, bid=Decimal("2.00")),
            _print(ts=datetime(2025, 7, 10, 15, tzinfo=UTC), strike=k100, bid=Decimal("2.50")),
            _print(ts=datetime(2025, 7, 10, 19, tzinfo=UTC), strike=k100, bid=Decimal("3.00")),
        ],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=k100,
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 10, 16, tzinfo=UTC),
    )
    # The 15:00 print, never the 19:00 one that comes after the exit.
    assert bid == Decimal("2.50")


def test_a_quote_from_another_session_is_refused_not_walked_back(tmp_path: Path) -> None:
    """Inverted by the 2026-09-19 audit. This test used to pin the leak as correct.

    It asserted that an exit on 2025-07-10 could be priced by a bid recorded on
    2025-06-20 — three weeks earlier, and in the real failure case BEFORE the
    position was opened, which booked a fabricated winner. Pinned decision #2 of
    ``docs/phase-3.5.0-acceptance.md`` says a missing bid leaves the trade open and
    is never fabricated; the guarded provider in ``simple_pnl.py`` has enforced
    that since 3.5.0.1, and this one now does too.
    """
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
    assert bid is None, "a bid from another day may not price this exit"


def test_an_earlier_day_in_the_same_month_is_refused(tmp_path: Path) -> None:
    """The same-day guard, pinned on its own.

    Written after noticing that neither of this file's other cases could fail if
    the guard were deleted: one uses only same-day prints, and the other's stale
    quote lives in a different month file, which the lookup no longer opens at
    all. A guard nothing can fail is the defect this whole change is about, so
    the quote here sits one day before the exit inside the SAME month.
    """
    k100 = Decimal("100")
    _write(
        tmp_path, "AMD", "2025-07",
        [_print(ts=datetime(2025, 7, 9, 15, tzinfo=UTC), strike=k100, bid=Decimal("2.50"))],
    )
    provider = ParquetExitQuoteProvider(tmp_path)
    bid = provider.get_bid(
        ticker="AMD", strike=k100,
        expiry=datetime(2025, 8, 15, tzinfo=UTC),
        option_type="call",
        at=datetime(2025, 7, 10, 16, tzinfo=UTC),
    )
    assert bid is None, "yesterday's bid may not price today's exit"


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
            # On the exit's own session: this test is about option_type and strike
            # disambiguation, not about staleness, so the dates must not make it
            # depend on the walk-back the audit removed.
            _print(ts=datetime(2025, 7, 20, tzinfo=UTC), strike=Decimal("100"), bid=Decimal("2.50"), option_type="call"),
            _print(ts=datetime(2025, 7, 20, tzinfo=UTC), strike=Decimal("100"), bid=Decimal("5.50"), option_type="put"),
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
