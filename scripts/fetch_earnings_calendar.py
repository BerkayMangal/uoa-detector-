"""Fetch the earnings calendar for the download universe (Phase 3.6.6).

Module 22 (event calendar) scores a flow event by its proximity to a
scheduled catalyst. Without Unusual Whales we source the catalyst dates
ourselves; earnings dates are public historical facts. This pulls them
from Yahoo Finance (yfinance — a build-time tool, NOT a runtime dep) and
writes a small committable CSV the CSVCatalystCalendarProvider reads.

Output: ``data/earnings_calendar.csv``
  ticker, when (ISO datetime, UTC), kind (earnings), title

The ``when`` is stamped 21:00 UTC (after the US close, the common earnings
release slot); M22 uses a ±30-day window so the exact time is not
material.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/fetch_earnings_calendar.py \\
        --out data/earnings_calendar.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def _tickers(bulk_dir: Path) -> list[str]:
    return sorted(p.name.upper() for p in bulk_dir.iterdir() if p.is_dir())


def _fetch(ticker: str) -> list[str]:
    """Return ISO dates (YYYY-MM-DD) of the ticker's earnings, or []."""
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=24)
    except Exception as exc:
        print(f"{ticker}: fetch failed: {exc!r}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []
    return sorted({d.strftime("%Y-%m-%d") for d in df.index})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch_earnings_calendar")
    parser.add_argument("--bulk-dir", type=Path, default=Path("data/historical/bulk"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", default="2025-04-01", help="keep dates >= this")
    parser.add_argument("--end", default="2026-05-31", help="keep dates <= this")
    args = parser.parse_args(argv)

    rows: list[tuple[str, str, str, str]] = []
    for ticker in _tickers(args.bulk_dir):
        for d in _fetch(ticker):
            if args.start <= d <= args.end:
                when = f"{d}T21:00:00+00:00"
                rows.append((ticker, when, "earnings", f"{ticker} earnings {d}"))
        print(f"{ticker}: {sum(1 for r in rows if r[0] == ticker)} earnings kept")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "when", "kind", "title"])
        w.writerows(rows)
    print(f"done: {len(rows)} earnings rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
