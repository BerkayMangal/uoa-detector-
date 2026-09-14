"""Phase 3.5.3.9 — bulk historical download driver.

The Phase 3.3.4 per-contract download driver issued one HTTP request
per (contract, day, endpoint): ~500 contracts × 21 days × 2 endpoints
≈ 21,000 calls for a single ticker-month. At ThetaData's sustained
~25 req/s that is ~15 minutes of pure round-trips per ticker-month —
the reason the Phase 3.5.3 download never finished in 11 days.

ThetaData v3's ``trade_quote`` history endpoint accepts ``expiration=*``
with no ``strike``/``right`` params, returning the WHOLE option chain
for a symbol on a given date in one response, with each trade row
carrying its at-trade bid/ask inline:

    GET /v3/option/history/trade_quote?symbol=AMD&expiration=*&date=YYYYMMDD
      → {"response": [
           {"contract": {"symbol","expiration","strike","right"},
            "data": [{trade_timestamp,price,size,sequence,...,
                      quote_timestamp,bid,ask,bid_size,ask_size}, ...]},
           ... one wrapper per contract ...
         ]}

So a ticker-day costs ONE call instead of ~1,000. The separate
``quote`` bulk endpoint is deliberately NOT used: an ``expiration=*``
quote bulk returns every quote tick of the whole chain (gigabytes,
hangs for tens of minutes); ``trade_quote`` returns only trade-count
rows. The full Phase 3.5.3 universe (24 tickers × 12 months × ~21
trading days × 1 call) is ~6,000 calls — minutes, not weeks.

Output layout matches ``historical_file_path``:
    {output_dir}/{TICKER}/{YYYY-MM}.parquet
which is exactly what ``ParquetReplaySource`` (Phase 3.5.5) expects —
the bulk path also resolves the per-contract-vs-per-ticker layout
mismatch for free.

Resume: a ticker-month whose parquet already exists with > 0 rows is
skipped. Re-running the same command is safe and cheap.

Snapshot (spot/OI/IV) is a no-op zero snapshot — same as the Phase
3.3.4 driver's ``_default_snapshot_for``; a real snapshot provider is
a Phase 3.4+ concern and does not block the download.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import logging
import math
import statistics
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.calibration import load_default_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.historical.universe import read_universe, tickers_only
from uoa_detector.sources._http_base import RetryPolicy
from uoa_detector.sources.thetadata.client import ThetaDataClient
from uoa_detector.sources.thetadata.historical import historical_file_path
from uoa_detector.sources.thetadata.mapping import (
    map_thetadata_trade_to_rawprint,
    trade_row_from_v3_dict,
)

_logger = logging.getLogger("download_bulk")

_ZERO = Decimal("0")

# Phase 3.5.5.13 (Track A4 via parity): static annualized risk-free
# rate for put-call parity spot derivation. Recent SOFR has hovered
# 4-5%; 5% gives a ~0.5% bias on K·exp(-rt) at DTE=60, well within
# the moneyness bands the convexity scoring cares about. Dividends
# are ignored — for div-payers this introduces a small additive bias
# (~$0.5-1 on a $150 stock) that is consistent across strikes and
# does not break parity's internal consistency.
_PARITY_RATE = 0.05


def _months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days_in_month(year: int, month: int, start: date, end: date) -> list[date]:
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days: list[date] = []
    d = max(first, start)
    last = min(nxt - timedelta(days=1), end)
    while d <= last:
        # Skip weekends — ThetaData returns empty for non-trading days
        # anyway; skipping saves two HTTP calls per weekend day.
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _wrappers(resp: object) -> list[dict[str, Any]]:
    """Pull the per-contract wrapper list out of a v3 bulk response."""
    if not isinstance(resp, dict):
        return []
    raw = resp.get("response")
    if not isinstance(raw, list):
        return []
    return [w for w in raw if isinstance(w, dict)]


_TRADE_QUOTE_ENDPOINT = "/v3/option/history/trade_quote"


async def _fetch_day(
    client: ThetaDataClient, ticker: str, day: date,
) -> list[dict[str, Any]]:
    """One bulk call: whole option chain trade+quote for ``ticker``/``day``.

    Phase 3.5.3.9: uses the ``trade_quote`` endpoint — every trade row
    carries its at-trade bid/ask inline (``bid``/``ask``/``bid_size``/
    ``ask_size``/``quote_timestamp``). No separate quote call, no
    quote-tick explosion (a plain ``quote`` bulk for a whole chain is
    gigabytes; ``trade_quote`` is only trade-count rows).
    """
    params = {
        "symbol": ticker.upper(),
        "expiration": "*",
        "date": day.strftime("%Y%m%d"),
    }
    resp = await client.request_json(_TRADE_QUOTE_ENDPOINT, params=params)
    return _wrappers(resp)


def _parse_trade_ts(s: str) -> datetime | None:
    """ThetaData v3 trade_timestamp ISO string → tz-aware UTC datetime.

    The string is ET-naive in the v3 feed; we parse and force UTC since
    that is what the rest of the pipeline expects. We do NOT convert ET
    → UTC here because the bulk-download mapping treats these as UTC
    consistently; what matters for parity is internal time-ordering,
    which is unchanged whichever fixed offset we apply.
    """
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _compute_parity_spot_curve(
    wrappers: list[dict[str, Any]],
    day: date,
) -> list[tuple[datetime, float]]:
    """Per-minute spot estimate curve from put-call parity pairs.

    Phase 3.5.5.13 (Track A4 via parity): for each (expiry, strike,
    minute) where BOTH a call and a put traded, compute a spot
    estimate via ``S = K·exp(-r·DTE/365) + C − P``. Within a minute,
    multiple (expiry, strike) pairs may give estimates; the per-minute
    spot is the median across pairs (robust to outliers / volatility
    skew). The result is sorted ascending by minute and is consumed by
    ``_spot_at`` (forward-fill lookup) for the trade-emission pass.

    Built from ALL wrappers irrespective of the day's max_dte filter —
    longer-dated options also reveal spot via parity and the curve
    benefits from the extra coverage.
    """
    # bucket[(expiry, strike, minute)][right] = [prices]
    bucket: dict[
        tuple[date, Decimal, datetime], dict[str, list[float]],
    ] = defaultdict(lambda: {"C": [], "P": []})
    for tw in wrappers:
        contract = tw.get("contract")
        if not isinstance(contract, dict):
            continue
        rows = tw.get("data")
        if not isinstance(rows, list):
            continue
        try:
            expiry = date.fromisoformat(str(contract["expiration"]))
            strike = Decimal(str(contract["strike"]))
            right_raw = str(contract["right"]).upper()
        except (KeyError, ValueError, TypeError):
            continue
        if right_raw not in ("CALL", "C", "PUT", "P"):
            continue
        right = "C" if right_raw in ("CALL", "C") else "P"
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts_raw = r.get("trade_timestamp") or r.get("quote_timestamp")
            price_raw = r.get("price")
            if ts_raw is None or price_raw is None:
                continue
            ts = _parse_trade_ts(str(ts_raw))
            if ts is None:
                continue
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                continue
            minute = ts.replace(second=0, microsecond=0)
            bucket[(expiry, strike, minute)][right].append(price)

    # Parity spot per (expiry, strike, minute) pair.
    minute_spots: dict[datetime, list[float]] = defaultdict(list)
    for (expiry, strike, minute), cp in bucket.items():
        if not cp["C"] or not cp["P"]:
            continue
        dte = (expiry - day).days
        if dte < 0:
            continue
        c = statistics.median(cp["C"])
        p = statistics.median(cp["P"])
        discount = math.exp(-_PARITY_RATE * dte / 365.0)
        spot = float(strike) * discount + c - p
        if spot > 0:  # sanity — negative spot is degenerate noise
            minute_spots[minute].append(spot)

    curve: list[tuple[datetime, float]] = []
    for minute, spots in minute_spots.items():
        curve.append((minute, statistics.median(spots)))
    curve.sort(key=lambda x: x[0])
    return curve


def _spot_at(
    curve: list[tuple[datetime, float]], t: datetime,
) -> Decimal | None:
    """Forward-fill spot lookup: the most recent estimate at or before ``t``.

    Returns None when ``t`` precedes the first available estimate of
    the day — the caller then falls back to ``Decimal('0')``, leaving
    ``spot_price=0`` on the print (convexity stays None for that print,
    which is graceful given the IV/OI nullable change).
    """
    if not curve:
        return None
    idx = bisect.bisect_right(curve, (t, math.inf))
    if idx == 0:
        return None
    _, spot = curve[idx - 1]
    return Decimal(f"{spot:.6f}")


def _day_to_prints(
    wrappers: list[dict[str, Any]],
    *,
    ticker: str,
    day: date,
    max_dte: int | None,
) -> list[RawPrint]:
    """Map a day's bulk trade_quote wrappers into RawPrints.

    Each row already pairs a trade with its at-trade quote (no align
    step needed — bid/ask are read straight off the row). Phase 3.5.5.13:
    spot_price is filled per-trade from a put-call parity curve built
    from the whole day's wrappers; trades before the first parity
    estimate of the day get spot_price=0 (graceful — convexity_score
    stays None for those).
    """
    curve = _compute_parity_spot_curve(wrappers, day)
    prints: list[RawPrint] = []
    for tw in wrappers:
        contract = tw.get("contract")
        if not isinstance(contract, dict):
            continue
        rows = tw.get("data")
        if not isinstance(rows, list):
            continue
        try:
            expiry = date.fromisoformat(str(contract["expiration"]))
            strike = Decimal(str(contract["strike"]))
            right_raw = str(contract["right"]).upper()
        except (KeyError, ValueError, TypeError):
            continue
        if right_raw not in ("CALL", "C", "PUT", "P"):
            continue
        right = "C" if right_raw in ("CALL", "C") else "P"
        # Day-relative DTE guard.
        if max_dte is not None:
            dte = (expiry - day).days
            if dte < 0 or dte > max_dte:
                continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                # trade_quote rows name the trade time 'trade_timestamp';
                # trade_row_from_v3_dict expects 'timestamp'.
                trade_dict = dict(r)
                if "trade_timestamp" in trade_dict:
                    trade_dict["timestamp"] = trade_dict["trade_timestamp"]
                trade = trade_row_from_v3_dict(trade_dict)
                bid = Decimal(str(r["bid"]))
                ask = Decimal(str(r["ask"]))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                continue
            trade_ts = _parse_trade_ts(str(r.get("trade_timestamp", "")))
            spot = (
                _spot_at(curve, trade_ts) if trade_ts is not None else None
            ) or _ZERO
            rp = map_thetadata_trade_to_rawprint(
                trade_row=trade,
                ticker=ticker,
                expiry=expiry,
                strike_dollars=strike,
                right=right,  # type: ignore[arg-type]
                spot_price=spot,
                bid=bid,
                ask=ask,
            )
            if rp is not None:
                prints.append(rp)
    return prints


def _month_complete(path: Path) -> bool:
    """True if the month's parquet already exists with > 0 rows."""
    if not path.exists():
        return False
    try:
        import pyarrow.parquet as pq
        return bool(pq.ParquetFile(path).metadata.num_rows > 0)  # type: ignore[no-untyped-call]
    except Exception:
        return False


async def _download_ticker_month(
    client: ThetaDataClient,
    ticker: str,
    year: int,
    month: int,
    *,
    start: date,
    end: date,
    output_dir: Path,
    max_dte: int | None,
) -> tuple[str, int]:
    """Download one ticker-month via bulk calls. Returns (status, rows).

    Streams day-by-day: each day's prints are converted to a parquet
    row group and written immediately via ``ParquetWriter``, then
    dropped. The whole month is NEVER materialised in memory — a
    high-volume ticker (TSLA) is tens of millions of rows per month,
    which OOM-kills the process if accumulated as RawPrint objects.

    Within-file event-time monotonicity (required by
    ``ParquetReplaySource``) holds because each day's prints are
    sorted before its row group is written and day N's prints all
    precede day N+1's.
    """
    import pyarrow.parquet as pq

    from uoa_detector.backtest.parquet_schema import (
        RAWPRINT_PARQUET_SCHEMA,
        raw_prints_to_table,
    )

    fp = historical_file_path(output_dir, ticker, year, month)
    if _month_complete(fp):
        return "skip", 0

    days = _days_in_month(year, month, start, end)
    fp.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    total = 0
    try:
        for d in days:
            try:
                wrappers = await _fetch_day(client, ticker, d)
            except Exception as exc:  # one bad day must not abort the month
                _logger.warning("%s %s: day fetch failed: %r", ticker, d, exc)
                continue
            day_prints = _day_to_prints(
                wrappers, ticker=ticker, day=d, max_dte=max_dte,
            )
            if not day_prints:
                continue
            day_prints.sort(key=lambda p: p.timestamp)
            table = raw_prints_to_table(day_prints)
            if writer is None:
                writer = pq.ParquetWriter(  # type: ignore[no-untyped-call]
                    fp, RAWPRINT_PARQUET_SCHEMA,
                    compression="zstd", compression_level=3,
                )
            writer.write_table(table)
            total += len(day_prints)
    finally:
        if writer is not None:
            writer.close()

    # A month with no rows at all (every day failed or empty): write an
    # empty file so the layout stays consistent. _month_complete treats
    # 0 rows as incomplete, so a re-run retries it.
    if writer is None:
        write_parquet([], fp)
    return "done", total


async def _run(args: argparse.Namespace) -> int:
    universe = read_universe(args.tier_csv)
    tickers = tickers_only(universe)
    _logger.info("universe: %d tickers from %s", len(tickers), args.tier_csv)

    creds = Credentials()
    api_key = creds.require_thetadata_api_key()
    profile = load_default_profile()
    client = ThetaDataClient(
        api_key=api_key,
        settings=profile.data_sources.thetadata,
        circuit_breaker_threshold=10**9,
        retry=RetryPolicy(max_attempts=5),
    )

    months = _months_in_range(args.start_date, args.end_date)
    total = len(tickers) * len(months)
    done = skipped = failed = 0
    total_rows = 0
    sem = asyncio.Semaphore(args.concurrency)

    async def _one(ticker: str, year: int, month: int) -> None:
        nonlocal done, skipped, failed, total_rows
        async with sem:
            try:
                status, rows = await _download_ticker_month(
                    client, ticker, year, month,
                    start=args.start_date, end=args.end_date,
                    output_dir=args.output_dir, max_dte=args.max_dte,
                )
            except Exception as exc:
                failed += 1
                _logger.error("%s %d-%02d FAILED: %r", ticker, year, month, exc)
                return
            if status == "skip":
                skipped += 1
            else:
                done += 1
                total_rows += rows
            n = done + skipped + failed
            _logger.info(
                "[%d/%d] %s %d-%02d: %s rows=%d",
                n, total, ticker, year, month, status, rows,
            )

    try:
        await asyncio.gather(
            *(
                _one(t, y, m)
                for t in tickers
                for (y, m) in months
            ),
        )
    finally:
        await client.aclose()

    _logger.info(
        "BULK DOWNLOAD COMPLETE: done=%d skipped=%d failed=%d rows=%d",
        done, skipped, failed, total_rows,
    )
    return 0 if failed == 0 else 1


def _iso(s: str) -> date:
    return date.fromisoformat(s)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="download_bulk")
    p.add_argument("--tier-csv", type=Path, required=True)
    p.add_argument("--start-date", type=_iso, required=True)
    p.add_argument("--end-date", type=_iso, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--max-dte", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
