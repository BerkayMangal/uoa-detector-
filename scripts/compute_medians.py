"""Compute per-ticker median trade premium from the bulk parquet data.

Phase 3.5.5.4: M37 (relative premium) scores a print's premium against
the ticker's median trade premium. ``data/medians.csv`` only carries a
5-ticker stub baseline; the Phase 3.5.5 backtest needs the 24-ticker
download universe. This script scans the bulk dataset and writes a
``medians`` CSV in the same schema ``CSVMedianTradeSizeProvider`` reads.

The median is a single static per-ticker value (the same shape as the
existing ``data/medians.csv``) — "the typical trade premium for this
ticker over the downloaded window". A print far above it is what M37
flags as unusually large.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/compute_medians.py \\
        --data-dir data/historical/bulk \\
        --out data/medians_bulk.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path


def _ticker_median(ticker_dir: Path) -> float | None:
    """Median of ``premium_paid`` across all of a ticker's month files."""
    import pyarrow.parquet as pq

    values: list[float] = []
    for parquet in sorted(ticker_dir.glob("*.parquet")):
        pf = pq.ParquetFile(parquet)  # type: ignore[no-untyped-call]
        for batch in pf.iter_batches(  # type: ignore[no-untyped-call]
            batch_size=131072, columns=["premium_paid"],
        ):
            values.extend(
                float(v) for v in batch.column("premium_paid").to_pylist()
                if v is not None
            )
    if not values:
        return None
    return statistics.median(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compute_medians")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir
    if not data_dir.exists():
        print(f"data dir not found: {data_dir}", file=sys.stderr)
        return 1

    rows: list[tuple[str, float]] = []
    for ticker_dir in sorted(data_dir.iterdir()):
        if not ticker_dir.is_dir():
            continue
        median = _ticker_median(ticker_dir)
        if median is None:
            print(f"  {ticker_dir.name}: no data, skipped", file=sys.stderr)
            continue
        rows.append((ticker_dir.name.upper(), median))
        print(f"  {ticker_dir.name.upper()}: median premium ${median:,.2f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "median_premium_usd_30d"])
        for ticker, median in rows:
            writer.writerow([ticker, f"{median:.2f}"])
    print(f"wrote {len(rows)} tickers to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
