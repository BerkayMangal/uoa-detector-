"""Build per-ticker intraday spot series (minute bars) from the bulk parquet.

Phase 3.6.5: Module 23 (price confirmation) needs intraday spot movement —
spot at the print vs spot a lookback-window earlier. The bulk parquet
carries a parity spot on every print; this reduces it to one row per
(ticker, minute): the mean parity spot in that minute (within a 1-minute
bar the parity spot barely varies, so the mean is the bar's spot — and it
is a vectorised, multi-threaded-safe aggregate, unlike "last").

Output (read by ``SpotSeries``): per ticker ``{dir}/{TICKER}.parquet``
  minute : timestamp[us, UTC]   (the minute bucket)
  spot   : float64

Usage:
    PYTHONPATH=src .venv/bin/python scripts/compute_spot_series.py \\
        --data-dir data/historical/bulk --out-dir data/spot_series
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_ticker(ticker_dir: Path) -> object | None:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    parts: list[object] = []
    for parquet in sorted(ticker_dir.glob("*.parquet")):
        t = pq.read_table(parquet, columns=["timestamp", "spot_price"])  # type: ignore[no-untyped-call]
        spot = pc.cast(t.column("spot_price"), pa.float64())
        minute = pc.floor_temporal(t.column("timestamp"), unit="minute")
        per = pa.table({"minute": minute, "spot": spot}).filter(
            pc.greater(spot, 0.0),
        )
        if per.num_rows == 0:
            continue
        grouped = per.group_by("minute").aggregate(
            [("spot", "mean")],
        ).rename_columns(["minute", "spot"])
        parts.append(grouped)
    if not parts:
        return None
    full = pa.concat_tables(parts)
    return full.group_by("minute").aggregate(
        [("spot", "mean")],
    ).rename_columns(["minute", "spot"]).sort_by("minute")


def main(argv: list[str] | None = None) -> int:
    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser(prog="compute_spot_series")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.data_dir.exists():
        print(f"data-dir not found: {args.data_dir}", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for ticker_dir in sorted(p for p in args.data_dir.iterdir() if p.is_dir()):
        table = _build_ticker(ticker_dir)
        if table is None:
            print(f"{ticker_dir.name}: no spot, skipped")
            continue
        pq.write_table(table, args.out_dir / f"{ticker_dir.name.upper()}.parquet")  # type: ignore[attr-defined]
        total += table.num_rows  # type: ignore[attr-defined]
        print(f"{ticker_dir.name.upper()}: {table.num_rows} minute bars")  # type: ignore[attr-defined]
    print(f"done: {total} bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
