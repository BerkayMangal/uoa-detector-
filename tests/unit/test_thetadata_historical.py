"""Phase 3.3.2.4 tests for ``thetadata.historical``.

All tests use a mock ThetaDataClient (subclassed downloader that
overrides _fetch_trades_for_day and _fetch_quotes_for_day) — no
real network. The acceptance doc names a 5-ticker × 1-month fixture
shape; we use 2-ticker × 2-month here because the unit test only
needs to pin orchestration semantics, not bulk volume.

Pins:
  - historical_file_path layout matches acceptance:
    {output_dir}/{TICKER}/{YYYY-MM}.parquet
  - align_quotes_to_trades does as-of merge correctly
  - _months_in_range produces correct (year, month) tuples
  - _days_in_month_for_range respects [start, end] bounds
  - download_request writes one parquet per month
  - parquet content matches RAWPRINT_PARQUET_SCHEMA
  - re-running skips months already on disk (idempotent)
  - empty parquet (0 rows) is treated as incomplete and re-downloaded
  - filtered trades (out-of-hours, drop conditions) excluded from output
  - snapshot_for_date is called once per trading day
  - quote alignment: trade with no preceding quote → dropped
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest.parquet_schema import RAWPRINT_PARQUET_SCHEMA
from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.sources.thetadata.client import ThetaDataClient
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
    DownloadRequest,
    QuoteRow,
    ThetaDataHistoricalDownloader,
    TradeRow,
    _days_in_month_for_range,
    _months_in_range,
    align_quotes_to_trades,
    historical_file_path,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_historical_file_path_layout(tmp_path: Path) -> None:
    """{output_dir}/{TICKER_UPPER}/{YYYY-MM}.parquet."""
    p = historical_file_path(tmp_path, "aapl", 2024, 7)
    assert p == tmp_path / "AAPL" / "2024-07.parquet"


def test_historical_file_path_zero_pads_month() -> None:
    p = historical_file_path(Path("/x"), "SPY", 2024, 1)
    assert str(p).endswith("/SPY/2024-01.parquet")


# ---------------------------------------------------------------------------
# Range / day helpers
# ---------------------------------------------------------------------------


def test_months_in_range_single_month() -> None:
    assert _months_in_range(date(2024, 1, 5), date(2024, 1, 25)) == [(2024, 1)]


def test_months_in_range_three_months() -> None:
    assert _months_in_range(date(2024, 1, 31), date(2024, 3, 1)) == [
        (2024, 1), (2024, 2), (2024, 3),
    ]


def test_months_in_range_year_boundary() -> None:
    assert _months_in_range(date(2024, 11, 1), date(2025, 2, 28)) == [
        (2024, 11), (2024, 12), (2025, 1), (2025, 2),
    ]


def test_months_in_range_inverted_raises() -> None:
    with pytest.raises(ValueError, match="before start"):
        _months_in_range(date(2024, 3, 1), date(2024, 1, 1))


def test_days_in_month_clipped_to_range() -> None:
    """Within the (year, month), only days in [start, end] inclusive."""
    days = _days_in_month_for_range(
        2024, 7, date(2024, 7, 5), date(2024, 7, 8),
    )
    assert days == [
        date(2024, 7, 5), date(2024, 7, 6),
        date(2024, 7, 7), date(2024, 7, 8),
    ]


def test_days_in_month_clipped_at_month_boundary() -> None:
    days = _days_in_month_for_range(
        2024, 7, date(2024, 7, 30), date(2024, 8, 5),
    )
    assert days == [date(2024, 7, 30), date(2024, 7, 31)]


# ---------------------------------------------------------------------------
# Quote alignment (as-of merge)
# ---------------------------------------------------------------------------


def _q(ms: int, bid: str = "1.00", ask: str = "1.05") -> QuoteRow:
    return QuoteRow(ms_of_day=ms, bid=Decimal(bid), ask=Decimal(ask))


def _t(ms: int) -> TradeRow:
    return TradeRow(
        ms_of_day=ms, sequence=1, condition=0, size=1,
        exchange=43, price=Decimal("1.02"), date=20240115,
    )


def test_align_quotes_picks_most_recent_at_or_before() -> None:
    quotes = [_q(100, "1.00", "1.05"), _q(200, "1.10", "1.15")]
    trades = [_t(150), _t(250)]
    aligned = align_quotes_to_trades(trades, quotes)
    # Trade at 150 → quote at 100 (most recent <=150)
    # Trade at 250 → quote at 200
    assert aligned[0] is not None and aligned[0].bid == Decimal("1.00")
    assert aligned[1] is not None and aligned[1].bid == Decimal("1.10")


def test_align_quotes_trade_before_first_quote() -> None:
    """Trade before any quote → None."""
    quotes = [_q(200)]
    trades = [_t(100)]
    aligned = align_quotes_to_trades(trades, quotes)
    assert aligned == [None]


def test_align_quotes_simultaneous_timestamp_uses_quote() -> None:
    """ms_of_day equality: quote at-the-trade-time IS at-or-before."""
    quotes = [_q(150)]
    trades = [_t(150)]
    aligned = align_quotes_to_trades(trades, quotes)
    assert aligned[0] is not None
    assert aligned[0].ms_of_day == 150


def test_align_quotes_empty_quote_list() -> None:
    aligned = align_quotes_to_trades([_t(100), _t(200)], [])
    assert aligned == [None, None]


def test_align_quotes_more_quotes_than_trades() -> None:
    quotes = [_q(100), _q(110), _q(120), _q(130), _q(200)]
    trades = [_t(150), _t(250)]
    aligned = align_quotes_to_trades(trades, quotes)
    assert aligned[0] is not None
    assert aligned[0].ms_of_day == 130  # last quote <= 150
    assert aligned[1] is not None
    assert aligned[1].ms_of_day == 200


# ---------------------------------------------------------------------------
# Download orchestration — mocked downloader
# ---------------------------------------------------------------------------


class _MockDownloader(ThetaDataHistoricalDownloader):
    """Subclass that overrides the network fetchers with fixture data."""

    def __init__(
        self,
        *,
        client: ThetaDataClient,
        trades_by_date: dict[date, list[TradeRow]],
        quotes_by_date: dict[date, list[QuoteRow]],
        concurrency: int = 4,
    ) -> None:
        super().__init__(client=client, concurrency=concurrency)
        self._trades_by_date = trades_by_date
        self._quotes_by_date = quotes_by_date
        self.trade_calls: list[date] = []
        self.quote_calls: list[date] = []

    async def _fetch_trades_for_day(
        self, contract: ContractSpec, day: date,
    ) -> list[TradeRow]:
        self.trade_calls.append(day)
        return self._trades_by_date.get(day, [])

    async def _fetch_quotes_for_day(
        self, contract: ContractSpec, day: date,
    ) -> list[QuoteRow]:
        self.quote_calls.append(day)
        return self._quotes_by_date.get(day, [])


def _make_client() -> ThetaDataClient:
    """Build a ThetaDataClient that the mock downloader doesn't actually call."""
    import httpx
    from pydantic import SecretStr
    return ThetaDataClient(
        api_key=SecretStr("test"),
        settings=ThetaDataSettings(rate_limit_requests_per_second=100.0),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )


def _midday_trade(ms: int = (14 * 3600) * 1000, *, date_int: int = 20240115) -> TradeRow:
    return TradeRow(
        ms_of_day=ms, sequence=ms, condition=0, size=2,
        exchange=43, price=Decimal("3.20"), date=date_int,
    )


def _midday_quote(
    ms: int = (14 * 3600) * 1000 - 100,
    *,
    bid: str = "3.18",
    ask: str = "3.22",
) -> QuoteRow:
    return QuoteRow(ms_of_day=ms, bid=Decimal(bid), ask=Decimal(ask))


@pytest.mark.asyncio
async def test_download_request_writes_parquet(tmp_path: Path) -> None:
    """Happy path: 1 day, 1 trade, 1 quote → 1 parquet file with 1 row."""
    client = _make_client()
    try:
        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 15),
            output_dir=tmp_path,
            snapshot_for_date=lambda _d: ContextSnapshot(
                spot_price=Decimal("180.00"), open_interest=12000,
                implied_volatility=0.28,
            ),
        )
        downloader = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [_midday_trade()]},
            quotes_by_date={date(2024, 1, 15): [_midday_quote()]},
        )
        results = await downloader.download_request(request)
        assert len(results) == 1
        r = results[0]
        assert r.skipped is False
        assert r.row_count == 1
        assert r.file_path == tmp_path / "AAPL" / "2024-01.parquet"
        assert r.file_path.exists()

        # Parquet content sanity
        table = pq.read_table(r.file_path)
        assert table.num_rows == 1
        assert table.schema.equals(RAWPRINT_PARQUET_SCHEMA)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_download_request_idempotent(tmp_path: Path) -> None:
    """Re-running with the same range skips months already on disk."""
    client = _make_client()
    try:
        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 15),
            output_dir=tmp_path,
            snapshot_for_date=lambda _d: ContextSnapshot(
                spot_price=Decimal("180.00"),
            ),
        )

        downloader_a = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [_midday_trade()]},
            quotes_by_date={date(2024, 1, 15): [_midday_quote()]},
        )
        a = await downloader_a.download_request(request)
        assert a[0].skipped is False
        assert downloader_a.trade_calls == [date(2024, 1, 15)]

        # Second run should NOT call any fetcher.
        downloader_b = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [_midday_trade()]},
            quotes_by_date={date(2024, 1, 15): [_midday_quote()]},
        )
        b = await downloader_b.download_request(request)
        assert b[0].skipped is True
        assert b[0].row_count == 1
        assert downloader_b.trade_calls == []  # no fetch on skip
        assert downloader_b.quote_calls == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_zero_row_parquet_is_re_downloaded(tmp_path: Path) -> None:
    """An empty parquet file (e.g., from a crashed earlier run) is
    treated as incomplete; the downloader regenerates it."""
    client = _make_client()
    try:
        # Pre-create a zero-row parquet at the target path to simulate
        # a crashed previous run.
        from uoa_detector.backtest.parquet_schema import write_parquet
        target = tmp_path / "AAPL" / "2024-01.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        write_parquet([], target)
        assert target.exists()

        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 15),
            output_dir=tmp_path,
            snapshot_for_date=lambda _d: ContextSnapshot(
                spot_price=Decimal("180.00"),
            ),
        )
        downloader = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [_midday_trade()]},
            quotes_by_date={date(2024, 1, 15): [_midday_quote()]},
        )
        results = await downloader.download_request(request)
        assert results[0].skipped is False
        assert results[0].row_count == 1
        assert downloader.trade_calls == [date(2024, 1, 15)]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_filtered_trades_excluded_from_output(tmp_path: Path) -> None:
    """Trades that map to None (out-of-hours, drop condition) are
    excluded from the parquet output."""
    client = _make_client()
    try:
        # Three trades: regular, extended-hours (18:00 ET), cancel.
        regular = _midday_trade(ms=(14 * 3600) * 1000)
        extended = TradeRow(
            ms_of_day=(18 * 3600) * 1000, sequence=2, condition=0,
            size=1, exchange=43, price=Decimal("3.20"), date=20240115,
        )
        cancelled = TradeRow(
            ms_of_day=(15 * 3600) * 1000, sequence=3, condition=7,  # cancel
            size=1, exchange=43, price=Decimal("3.20"), date=20240115,
        )

        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 15),
            output_dir=tmp_path,
            snapshot_for_date=lambda _d: ContextSnapshot(
                spot_price=Decimal("180.00"),
            ),
        )

        # Pre-quote at 13:00 ET so the regular trade has a quote
        # available, the cancelled trade also has one (condition
        # filter still applies), and the extended-hours trade
        # has the same quote (time filter still applies).
        quote = QuoteRow(
            ms_of_day=(13 * 3600) * 1000,
            bid=Decimal("3.18"), ask=Decimal("3.22"),
        )

        downloader = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [regular, cancelled, extended]},
            quotes_by_date={date(2024, 1, 15): [quote]},
        )
        results = await downloader.download_request(request)
        # Only the regular trade survives.
        assert results[0].row_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_snapshot_called_once_per_day(tmp_path: Path) -> None:
    """snapshot_for_date is invoked once per trading day in the range,
    not once per trade."""
    snap_calls: list[date] = []

    def snap(d: date) -> ContextSnapshot:
        snap_calls.append(d)
        return ContextSnapshot(spot_price=Decimal("180.00"))

    client = _make_client()
    try:
        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        # 3-day range
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 17),
            output_dir=tmp_path,
            snapshot_for_date=snap,
        )

        # Each day has 2 trades and 1 quote
        trades_per_day = {
            d: [_midday_trade(date_int=int(d.strftime("%Y%m%d"))),
                _midday_trade(
                    ms=(15 * 3600) * 1000,
                    date_int=int(d.strftime("%Y%m%d")),
                )]
            for d in [date(2024, 1, 15), date(2024, 1, 16), date(2024, 1, 17)]
        }
        quotes_per_day = {
            d: [_midday_quote(ms=(13 * 3600) * 1000)]
            for d in [date(2024, 1, 15), date(2024, 1, 16), date(2024, 1, 17)]
        }

        downloader = _MockDownloader(
            client=client,
            trades_by_date=trades_per_day,
            quotes_by_date=quotes_per_day,
        )
        await downloader.download_request(request)
        # Three days, three snapshot calls (one per day, not per trade)
        assert snap_calls == [
            date(2024, 1, 15), date(2024, 1, 16), date(2024, 1, 17),
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_snapshot_supported(tmp_path: Path) -> None:
    """snapshot_for_date can return a coroutine (real production case)."""
    async def async_snap(_d: date) -> ContextSnapshot:
        return ContextSnapshot(spot_price=Decimal("180.00"))

    client = _make_client()
    try:
        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 2, 16),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 15), end_date=date(2024, 1, 15),
            output_dir=tmp_path,
            snapshot_for_date=async_snap,
        )
        downloader = _MockDownloader(
            client=client,
            trades_by_date={date(2024, 1, 15): [_midday_trade()]},
            quotes_by_date={date(2024, 1, 15): [_midday_quote()]},
        )
        results = await downloader.download_request(request)
        assert results[0].row_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_multi_month_range_writes_one_parquet_per_month(
    tmp_path: Path,
) -> None:
    """A 2-month range writes 2 parquet files."""
    client = _make_client()
    try:
        contract = ContractSpec(
            ticker="AAPL", expiry=date(2024, 5, 17),
            strike_dollars=Decimal("170.00"), right="C",
        )
        request = DownloadRequest(
            contract=contract,
            start_date=date(2024, 1, 30), end_date=date(2024, 2, 2),
            output_dir=tmp_path,
            snapshot_for_date=lambda _d: ContextSnapshot(
                spot_price=Decimal("180.00"),
            ),
        )
        # One trade per day
        trades_by_date = {
            d: [_midday_trade(date_int=int(d.strftime("%Y%m%d")))]
            for d in [
                date(2024, 1, 30), date(2024, 1, 31),
                date(2024, 2, 1), date(2024, 2, 2),
            ]
        }
        quotes_by_date = {
            d: [_midday_quote(ms=(13 * 3600) * 1000)]
            for d in trades_by_date
        }

        downloader = _MockDownloader(
            client=client,
            trades_by_date=trades_by_date,
            quotes_by_date=quotes_by_date,
        )
        results = await downloader.download_request(request)
        assert len(results) == 2  # Jan and Feb
        files = sorted(r.file_path.name for r in results)
        assert files == ["2024-01.parquet", "2024-02.parquet"]
        # Jan: 2 days × 1 trade = 2 rows
        # Feb: 2 days × 1 trade = 2 rows
        rows_by_month = {r.file_path.name: r.row_count for r in results}
        assert rows_by_month["2024-01.parquet"] == 2
        assert rows_by_month["2024-02.parquet"] == 2
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Decoder tests — full JSON response shape
# ---------------------------------------------------------------------------


def test_decode_trade_response_handles_documented_shape() -> None:
    """The ThetaData docs example shape decodes correctly."""
    from uoa_detector.sources.thetadata.historical import _decode_trade_response

    response: dict[str, Any] = {
        "header": {"format": ["ms_of_day", "sequence", "ext_condition1",
                              "ext_condition2", "ext_condition3", "ext_condition4",
                              "condition", "size", "exchange", "price",
                              "condition_flags", "price_flags", "volume_type",
                              "records_back", "date"]},
        "response": [
            [43860664, 602567584, 255, 255, 255, 255, 0, 1, 43, 5.84,
             0, 1, 0, 0, 20240116],
            [50000000, 602567585, 255, 255, 255, 255, 0, 5, 50, 5.85,
             0, 1, 0, 0, 20240116],
        ],
    }
    rows = _decode_trade_response(response)
    assert len(rows) == 2
    assert rows[0].ms_of_day == 43860664
    assert rows[1].sequence == 602567585


def test_decode_trade_response_empty() -> None:
    from uoa_detector.sources.thetadata.historical import _decode_trade_response
    rows = _decode_trade_response({"response": []})
    assert rows == []


def test_decode_quote_response_decodes() -> None:
    from uoa_detector.sources.thetadata.historical import _decode_quote_response
    response: dict[str, Any] = {
        "response": [
            [43860664, 10, 14, 5.83, 0, 15, 14, 5.85, 0, 20240116],
        ],
    }
    rows = _decode_quote_response(response)
    assert len(rows) == 1
    assert rows[0].bid == Decimal("5.83")
    assert rows[0].ask == Decimal("5.85")


def test_decode_quote_response_v3_nested_wrapper() -> None:
    """Phase 3.3.11: real ThetaData v3 wraps rows inside per-contract
    objects: ``{"response": [{"contract": {...}, "data": [rows]}]}``.
    The decoder flattens this shape before per-row v3-dict parsing.
    """
    from uoa_detector.sources.thetadata.historical import _decode_quote_response
    response: dict[str, Any] = {
        "response": [
            {
                "contract": {
                    "right": "CALL", "expiration": "2026-05-15",
                    "symbol": "AAPL", "strike": 290.000,
                },
                "data": [
                    {
                        "ask": 4.95, "bid": 3.35,
                        "ask_size": 2, "bid_size": 2,
                        "ask_exchange": 73, "bid_exchange": 46,
                        "ask_condition": 50, "bid_condition": 50,
                        "timestamp": "2026-05-08T09:30:01.000",
                    },
                ],
            },
        ],
    }
    rows = _decode_quote_response(response)
    assert len(rows) == 1
    assert rows[0].bid == Decimal("3.35")
    assert rows[0].ask == Decimal("4.95")


def test_decode_trade_response_v3_nested_wrapper() -> None:
    """Phase 3.3.11: same per-contract wrapper applies to /history/trade."""
    from uoa_detector.sources.thetadata.historical import _decode_trade_response
    response: dict[str, Any] = {
        "response": [
            {
                "contract": {
                    "right": "CALL", "expiration": "2026-05-15",
                    "symbol": "AAPL", "strike": 290.000,
                },
                "data": [
                    {
                        "sequence": 959510345,
                        "condition": 125,
                        "size": 1, "price": 4.18,
                        "ext_condition1": 255, "ext_condition2": 255,
                        "ext_condition3": 255, "ext_condition4": 255,
                        "exchange": 43,
                        "timestamp": "2026-05-08T09:30:01.289",
                    },
                ],
            },
        ],
    }
    rows = _decode_trade_response(response)
    assert len(rows) == 1
    assert rows[0].price == Decimal("4.18")
    assert rows[0].size == 1
