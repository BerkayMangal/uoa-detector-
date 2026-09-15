"""Download daily chain snapshots (OI + BS-inverted IV) from ThetaData v3.

Phase 3.6.2. The bulk trade_quote download has no OI/IV, so the GEX
provider's chain comes from here: per ticker, fetch the full-chain daily
open interest and the eod bid/ask from ThetaData v3 (``expiration=*``),
join them, Black-Scholes-invert IV from the eod mid, take parity spot from
the existing bulk data, and write one snapshot parquet per ticker in the
schema ``DailyChainSnapshotSource`` reads.

The download is chain metadata, not per-trade — a couple of calls per
ticker-month. Requires ThetaTerminal running locally.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/download_chain_snapshots.py \\
        --tickers JNJ \\
        --start 2025-05 --end 2026-04 \\
        --bulk-dir data/historical/bulk \\
        --out-dir data/chain_snapshots
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

from uoa_detector.sources.thetadata_derived.black_scholes import implied_vol

_DEFAULT_BASE = "http://127.0.0.1:25503"


def _months(start: str, end: str) -> list[str]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    start = date(y, m, 1)
    first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    last = date.fromordinal(first_next.toordinal() - 1)
    return start.strftime("%Y%m%d"), last.strftime("%Y%m%d")


def _fetch(client: httpx.Client, base: str, endpoint: str, symbol: str, d0: str, d1: str) -> list[dict[str, str]]:
    r = client.get(
        f"{base}/v3/option/history/{endpoint}",
        params={"symbol": symbol, "expiration": "*", "start_date": d0, "end_date": d1},
        timeout=120,
    )
    if r.status_code != 200:
        return []
    return list(csv.DictReader(io.StringIO(r.text)))


def _row_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _spot_by_date(bulk_dir: Path, ticker: str) -> dict[date, float]:
    """Last parity spot per trading day from the bulk parquet."""
    import pyarrow.parquet as pq

    out: dict[date, float] = {}
    tdir = bulk_dir / ticker.upper()
    for parquet in sorted(tdir.glob("*.parquet")):
        pf = pq.ParquetFile(parquet)  # type: ignore[no-untyped-call]
        for batch in pf.iter_batches(  # type: ignore[no-untyped-call]
            batch_size=131072, columns=["timestamp", "spot_price"],
        ):
            for ts, sp in zip(
                batch.column("timestamp").to_pylist(),
                batch.column("spot_price").to_pylist(),
                strict=True,
            ):
                if sp is not None and float(sp) > 0.0:
                    out[ts.date()] = float(sp)  # later row overwrites → day's last
    return out


def _build_ticker(
    client: httpx.Client, base: str, ticker: str, months: list[str],
    spot_by_date: dict[date, float],
) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {
        "snapshot_date": [], "strike": [], "expiry": [], "option_type": [],
        "open_interest": [], "implied_volatility": [], "spot": [],
    }
    for month in months:
        d0, d1 = _month_bounds(month)
        oi_rows = _fetch(client, base, "open_interest", ticker, d0, d1)
        eod_rows = _fetch(client, base, "eod", ticker, d0, d1)
        # OI keyed by (date, expiration, strike, right)
        oi_map: dict[tuple[date, str, str, str], int] = {}
        for r in oi_rows:
            oi = r.get("open_interest")
            if not oi:
                continue
            oi_map[(_row_date(r["timestamp"]), r["expiration"], r["strike"], r["right"])] = int(oi)
        for r in eod_rows:
            try:
                d = _row_date(r["created"])
                key = (d, r["expiration"], r["strike"], r["right"])
                oi = oi_map.get(key)
                if not oi:
                    continue
                spot = spot_by_date.get(d)
                if spot is None or spot <= 0.0:
                    continue
                bid, ask = float(r["bid"]), float(r["ask"])
                if bid <= 0.0 or ask <= 0.0:
                    continue
                mid = 0.5 * (bid + ask)
                strike = float(r["strike"])
                expiry = date.fromisoformat(r["expiration"])
                t_years = (expiry - d).days / 365.0
                if t_years <= 0.0:
                    continue
                call = r["right"] == "CALL"
                iv = implied_vol(mid, spot, strike, t_years, call=call)
                if iv is None:
                    continue
                out["snapshot_date"].append(d)
                out["strike"].append(strike)
                out["expiry"].append(expiry)
                out["option_type"].append("call" if call else "put")
                out["open_interest"].append(oi)
                out["implied_volatility"].append(iv)
                out["spot"].append(spot)
            except (ValueError, KeyError):
                continue
    return out


def _write(out: dict[str, list[object]], path: Path) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({
        "snapshot_date": pa.array(out["snapshot_date"], type=pa.date32()),
        "strike": pa.array(out["strike"], type=pa.float64()),
        "expiry": pa.array(out["expiry"], type=pa.date32()),
        "option_type": pa.array(out["option_type"], type=pa.string()),
        "open_interest": pa.array(out["open_interest"], type=pa.int64()),
        "implied_volatility": pa.array(out["implied_volatility"], type=pa.float64()),
        "spot": pa.array(out["spot"], type=pa.float64()),
    })
    pq.write_table(table, path)
    return table.num_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="download_chain_snapshots")
    parser.add_argument("--tickers", required=True, help="comma-separated, e.g. JNJ,AMD")
    parser.add_argument("--start", required=True, help="YYYY-MM")
    parser.add_argument("--end", required=True, help="YYYY-MM")
    parser.add_argument("--bulk-dir", type=Path, default=Path("data/historical/bulk"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=_DEFAULT_BASE)
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    months = _months(args.start, args.end)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    with httpx.Client() as client:
        for ticker in tickers:
            spots = _spot_by_date(args.bulk_dir, ticker)
            if not spots:
                print(f"{ticker}: no bulk spot data, skipped", file=sys.stderr)
                continue
            out = _build_ticker(client, args.base_url, ticker, months, spots)
            n = _write(out, args.out_dir / f"{ticker}.parquet")
            days = len(set(out["snapshot_date"]))
            print(f"{ticker}: {n} contract-days across {days} days")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
