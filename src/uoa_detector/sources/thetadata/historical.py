"""ThetaData historical bulk downloader.

Phase 3.3.2.4. Given a contract spec (ticker, expiry, strike, right)
and a date range, fetches OPRA trades + quotes via the
``ThetaDataClient``, aligns quotes to trades by 'as-of' timestamp,
maps to ``RawPrint``, and writes one parquet file per month under
``output_dir/{ticker}/{YYYY-MM}.parquet``.

Idempotent: re-running the same range skips months whose parquet
file already exists with row_count > 0. The Phase 3.3.4 tier-2
download script chains this downloader across 51 tickers × 24
months with profile-tunable concurrency.

decision (one parquet per ticker × month):
  Acceptance doc names this layout:
  ``data/historical/thetadata/{ticker}/{YYYY-MM}.parquet``. Monthly
  granularity matches typical query / restore patterns and keeps
  individual files in the 10-200MB range — manageable for diff
  inspection, parquet column-pruning, and accidental-deletion
  recovery.

decision (idempotency = file presence + row_count > 0):
  Acceptance doc literal pin: 're-running the same range skips
  months already on disk (verified by file presence + row-count
  check)'. row_count > 0 protects against zero-byte stub files
  left by a crashed earlier run; file presence alone would skip
  those incorrectly.

decision (quote alignment = as-of lookup, sorted-merge):
  ThetaData publishes trades and quotes as separate streams.
  Mapping needs (bid, ask) at-trade-time. We sort both streams by
  ms_of_day (already sorted as returned by ThetaData), walk the
  trade list, and at each trade take the most-recent quote with
  ms_of_day <= trade.ms_of_day. O(n+m) merge, no per-trade lookup.

decision (spot price snapshot per request, not per trade):
  ThetaData exposes spot via a separate stock-quote endpoint. For
  3.3.2.4 we accept ``spot_price`` as a constructor parameter
  (caller supplies via the orchestration script). Phase 3.3.4
  will fetch ``snapshot/stock/quote`` once per ticker × month and
  pass that here. Eventually a finer-grained spot stream is a
  Phase 3.4 enhancement; the historical downloader is not the
  right layer to own that.

decision (one contract per call):
  ThetaData's hist/option/trade endpoint takes (root, exp, strike,
  right) — one contract per request. A liquid ticker has hundreds
  of (expiry, strike) combos; the bulk-download script (3.3.4)
  drives the contract enumeration. The downloader here owns one
  contract × time-range.

decision (open_interest + IV from sibling snapshot calls):
  Both are slow-changing; we fetch one snapshot per (ticker,
  expiry, strike, right) per day (not per trade) and apply that
  as a constant within the day. The constructor takes a callable
  ``snapshot_provider`` for testability — production wires
  through the actual ThetaDataClient endpoints in Phase 3.3.4.

decision (no spot price endpoint call here — caller supplies):
  Same reasoning — separation of concerns. The
  ``DownloadRequest`` dataclass holds the spot snapshot for the
  contract's underlying as a Decimal; caller is responsible for
  refreshing it (typically once per ticker × month).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.sources.thetadata.mapping import (
    QuoteRow,
    TradeRow,
    map_thetadata_trade_to_rawprint,
    quote_row_from_array,
    thetadata_dollars_to_strike,
    trade_row_from_array,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from decimal import Decimal

    from uoa_detector.domain.raw_print import RawPrint
    from uoa_detector.sources.thetadata.client import ThetaDataClient
    from uoa_detector.sources.thetadata.mapping import OptionRight


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    """One ThetaData option contract.

    All fields are required; the historical downloader fetches
    trades for exactly this contract.
    """

    ticker: str
    expiry: date
    strike_dollars: Decimal
    right: OptionRight  # 'C' or 'P'


@dataclass(frozen=True)
class ContextSnapshot:
    """Daily context attached to all trades on a given date.

    These come from sibling endpoints (stock quote, OI snapshot,
    IV snapshot). The downloader doesn't fetch them itself in
    3.3.2.4; the caller (or 3.3.4's orchestration script) supplies
    a function that returns one snapshot per (contract, date).
    """

    spot_price: Decimal
    open_interest: int | None = None
    implied_volatility: float | None = None


@dataclass(frozen=True)
class DownloadRequest:
    """A complete download request: contract + date range + context.

    ``snapshot_for_date`` returns the ``ContextSnapshot`` for the
    contract on the given date. The downloader calls it once per
    trading day in the range. Production wires this to real
    ThetaData endpoints; tests pass a synchronous lambda.
    """

    contract: ContractSpec
    start_date: date
    end_date: date
    output_dir: Path
    snapshot_for_date: Callable[[date], Awaitable[ContextSnapshot] | ContextSnapshot]


@dataclass(frozen=True)
class DownloadResult:
    """Per-month outcome.

    ``skipped`` is True when the file already existed (idempotent
    behaviour). ``row_count`` is the number of RawPrint records in
    the file (whether freshly written or pre-existing).
    """

    contract: ContractSpec
    year: int
    month: int
    file_path: Path
    skipped: bool
    row_count: int


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def historical_file_path(
    output_dir: Path, ticker: str, year: int, month: int,
) -> Path:
    """Compose the canonical monthly parquet path for a ticker.

    Layout per acceptance doc:
        {output_dir}/{ticker}/{YYYY-MM}.parquet

    The ``output_dir`` will typically be
    ``data/historical/thetadata`` from the tier-2 script.
    """
    ymm = f"{year:04d}-{month:02d}"
    return output_dir / ticker.upper() / f"{ymm}.parquet"


def _is_complete_file(path: Path) -> tuple[bool, int]:
    """Return (is_complete, row_count) for a parquet file.

    Idempotency check: file exists AND has > 0 rows.
    """
    if not path.exists():
        return False, 0
    try:
        pf = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
        num_rows: int = pf.metadata.num_rows
        return num_rows > 0, num_rows
    except Exception:
        return False, 0


# ---------------------------------------------------------------------------
# Quote alignment
# ---------------------------------------------------------------------------


def align_quotes_to_trades(
    trades: list[TradeRow],
    quotes: list[QuoteRow],
) -> list[QuoteRow | None]:
    """For each trade, return the most-recent quote at-or-before its time.

    ``None`` if no quote is at-or-before the trade (very early in
    the day, before the first quote — unusual but defensive).

    Both inputs must already be sorted by ``ms_of_day`` ascending,
    which they are as returned by ThetaData.
    """
    aligned: list[QuoteRow | None] = []
    j = 0
    last_quote: QuoteRow | None = None
    for trade in trades:
        # Advance the quote pointer while quotes[j].ms_of_day <= trade.ms_of_day
        while j < len(quotes) and quotes[j].ms_of_day <= trade.ms_of_day:
            last_quote = quotes[j]
            j += 1
        aligned.append(last_quote)
    return aligned


# ---------------------------------------------------------------------------
# Client interface (kept narrow so tests can pass a fake)
# ---------------------------------------------------------------------------


class _FetcherProtocol:
    """Documentation of the fetcher callable shape.

    The downloader is parameterised by a fetcher that retrieves a
    day's worth of trades / quotes for a contract. The default
    implementation wraps ``ThetaDataClient.request_json``; tests
    pass a synchronous fixture-driven fetcher.
    """


# ---------------------------------------------------------------------------
# Historical downloader
# ---------------------------------------------------------------------------


class ThetaDataHistoricalDownloader:
    """Fetches OPRA trade/quote data for one contract × date range.

    Lifecycle:
      dl = ThetaDataHistoricalDownloader(
          client=ThetaDataClient(...),
          concurrency=4,
      )
      result = await dl.download_request(req)

    The class is stateless beyond the client + concurrency limit;
    one instance can serve many concurrent download_request() calls
    bounded by the semaphore.
    """

    def __init__(
        self,
        *,
        client: ThetaDataClient,
        concurrency: int = 4,
    ) -> None:
        self._client = client
        self._sem = asyncio.Semaphore(max(1, concurrency))

    async def _fetch_trades_for_day(
        self, contract: ContractSpec, day: date,
    ) -> list[TradeRow]:
        """Default fetcher: real ThetaData call.

        Tests bypass this method by subclassing and overriding;
        production uses it directly. The endpoint and the response
        shape are pinned in the acceptance doc + ThetaData
        documentation.
        """
        params = {
            "root": contract.ticker.upper(),
            "exp": _yyyymmdd(contract.expiry),
            "strike": str(thetadata_dollars_to_strike(contract.strike_dollars)),
            "right": contract.right,
            "start_date": _yyyymmdd(day),
            "end_date": _yyyymmdd(day),
        }
        async with self._sem:
            response = await self._client.request_json(
                "/v2/hist/option/trade", params=params,
            )
        return _decode_trade_response(response)

    async def _fetch_quotes_for_day(
        self, contract: ContractSpec, day: date,
    ) -> list[QuoteRow]:
        """Default fetcher: real ThetaData quote call."""
        params = {
            "root": contract.ticker.upper(),
            "exp": _yyyymmdd(contract.expiry),
            "strike": str(thetadata_dollars_to_strike(contract.strike_dollars)),
            "right": contract.right,
            "start_date": _yyyymmdd(day),
            "end_date": _yyyymmdd(day),
        }
        async with self._sem:
            response = await self._client.request_json(
                "/v2/hist/option/quote", params=params,
            )
        return _decode_quote_response(response)

    async def _fetch_one_day(
        self, contract: ContractSpec, day: date, snapshot: ContextSnapshot,
    ) -> list[RawPrint]:
        """Fetch trades + quotes for one day, align, map to RawPrints.

        Trades whose mapping returns None (filtered: cancels, OOS,
        extended hours, negative DTE) are dropped from the output.
        """
        trades_task = asyncio.create_task(
            self._fetch_trades_for_day(contract, day),
        )
        quotes_task = asyncio.create_task(
            self._fetch_quotes_for_day(contract, day),
        )
        trades = await trades_task
        quotes = await quotes_task

        aligned = align_quotes_to_trades(trades, quotes)

        prints: list[RawPrint] = []
        for trade, quote in zip(trades, aligned, strict=True):
            if quote is None:
                # No quote available at-or-before this trade — skip.
                continue
            rp = map_thetadata_trade_to_rawprint(
                trade_row=trade,
                ticker=contract.ticker,
                expiry=contract.expiry,
                strike_dollars=contract.strike_dollars,
                right=contract.right,
                spot_price=snapshot.spot_price,
                bid=quote.bid,
                ask=quote.ask,
                open_interest=snapshot.open_interest,
                implied_volatility=snapshot.implied_volatility,
            )
            if rp is not None:
                prints.append(rp)
        return prints

    async def download_request(
        self, request: DownloadRequest,
    ) -> list[DownloadResult]:
        """Download the full range, writing one parquet per month.

        Returns a list of ``DownloadResult``, one per (ticker, year,
        month) combination touched. Skipped entries (file already
        complete) come back with ``skipped=True`` and the existing
        row count.
        """
        results: list[DownloadResult] = []
        for year, month in _months_in_range(
            request.start_date, request.end_date,
        ):
            file_path = historical_file_path(
                request.output_dir, request.contract.ticker, year, month,
            )
            complete, row_count = _is_complete_file(file_path)
            if complete:
                _logger.info(
                    "Skipping %s — already on disk with %d rows",
                    file_path, row_count,
                )
                results.append(DownloadResult(
                    contract=request.contract,
                    year=year, month=month,
                    file_path=file_path,
                    skipped=True,
                    row_count=row_count,
                ))
                continue

            # Fetch every day in the month that's also in the
            # request range.
            days_in_month = _days_in_month_for_range(
                year, month,
                request.start_date, request.end_date,
            )
            month_prints: list[RawPrint] = []
            for day in days_in_month:
                snapshot = await _resolve_snapshot(
                    request.snapshot_for_date, day,
                )
                day_prints = await self._fetch_one_day(
                    request.contract, day, snapshot,
                )
                month_prints.extend(day_prints)

            file_path.parent.mkdir(parents=True, exist_ok=True)
            write_parquet(month_prints, file_path)
            results.append(DownloadResult(
                contract=request.contract,
                year=year, month=month,
                file_path=file_path,
                skipped=False,
                row_count=len(month_prints),
            ))
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yyyymmdd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


def _months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    """List (year, month) tuples from start through end, inclusive."""
    if end < start:
        msg = f"end ({end}) before start ({start})"
        raise ValueError(msg)
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _days_in_month_for_range(
    year: int, month: int, start: date, end: date,
) -> list[date]:
    """Calendar days within the month-of-(year,month) AND the [start,end] range.

    No trading-calendar logic here — the caller can filter
    weekends/holidays if desired. For 3.3.2.4 we keep this simple
    and rely on ThetaData returning empty lists for non-trading
    days; Phase 3.3.4 may add a calendar overlay.
    """
    days: list[date] = []
    d = date(year, month, 1)
    while d.month == month and d <= end:
        if d >= start:
            days.append(d)
        d += timedelta(days=1)
    return days


async def _resolve_snapshot(
    fn: Callable[[date], Awaitable[ContextSnapshot] | ContextSnapshot],
    day: date,
) -> ContextSnapshot:
    """Call ``fn(day)``; await result if it's a coroutine, otherwise return."""
    result = fn(day)
    if asyncio.iscoroutine(result):
        snap: ContextSnapshot = await result
        return snap
    assert isinstance(result, ContextSnapshot)  # narrows for mypy
    return result


def _decode_trade_response(response: dict[str, object]) -> list[TradeRow]:
    """Parse a ThetaData trade endpoint response into TradeRow list.

    Response shape:
      {
        "header": {"format": ["ms_of_day", ..., "date"]},
        "response": [[...], [...], ...]
      }
    """
    rows = response.get("response", [])
    if not isinstance(rows, list):
        msg = f"ThetaData trade response.response is not a list: {type(rows).__name__}"
        raise TypeError(msg)
    out: list[TradeRow] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        out.append(trade_row_from_array(row))
    return out


def _decode_quote_response(response: dict[str, object]) -> list[QuoteRow]:
    """Parse a ThetaData quote endpoint response into QuoteRow list."""
    rows = response.get("response", [])
    if not isinstance(rows, list):
        msg = f"ThetaData quote response.response is not a list: {type(rows).__name__}"
        raise TypeError(msg)
    out: list[QuoteRow] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        out.append(quote_row_from_array(row))
    return out
