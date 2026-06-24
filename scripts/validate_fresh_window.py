"""Phase 1 data-quality gate for the Study D fresh window (pre-reg §8).

Metadata-only validation — reads counts/dates from the downloaded parquet, NO
study statistic is computed (this is not the D run). It answers the questions
the pre-reg makes binding before D may run:

  * Are all 24 universe tickers present and non-empty in BOTH chain_snapshots
    and spot_series? (a missing/empty ticker = broken universe = invalid test)
  * Does each ticker's snapshot coverage span 2024-05-01..2025-04-30, and how
    many trading days are MISSING (contiguity gaps — e.g. heavy days dropped on
    a JSONDecodeError during the bulk download)?
  * How many panel-ELIGIBLE observations does each ticker yield — snapshot
    dates that have a spot close AND a +20-trading-day forward close? This is
    the honest size of the test before any conditioning.

Run after the download completes:
    PYTHONPATH=src:. .venv/bin/python scripts/validate_fresh_window.py \
        --chains data/chain_snapshots_2024 --spots data/spot_series_2024

Exit code 0 = clean (all 24 present, no empty); 1 = at least one ticker
missing/empty (do NOT run D on a broken universe).
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

# Frozen pre-reg universe (docs/preregister_D.md §1) — order-independent.
_UNIVERSE = sorted([
    "ABNB", "AI", "AMD", "BAC", "COIN", "CRWD", "DKNG", "GOOGL", "HOOD", "JNJ",
    "JPM", "LCID", "MARA", "META", "PLTR", "PLUG", "RBLX", "RIOT", "RIVN",
    "SNAP", "SOFI", "TSLA", "UNH", "XOM",
])
_WIN_START = date(2024, 5, 1)
_WIN_END = date(2025, 4, 30)
_HOLD = 20  # pre-reg: +20 trading days for the forward close


def _expected_trading_days() -> pd.DatetimeIndex:
    """Weekdays in the window. Holidays are NOT modeled (≈9-10 will look
    'missing' per ticker and that is normal); the count is for comparison
    ACROSS tickers, where an outlier reveals a real gap, not for an exact
    holiday-aware target."""
    return pd.bdate_range(_WIN_START, _WIN_END)


def _daily_close(spot_path: Path) -> pd.Series:
    """Last spot per calendar day from the minute series (mirrors
    edge_validation._daily_close semantics: one close per trading day)."""
    s = pd.read_parquet(spot_path)
    s["day"] = pd.to_datetime(s["minute"]).dt.normalize()
    return s.groupby("day")["spot"].last()


def _validate_ticker(ticker: str, chains: Path, spots: Path) -> dict[str, object]:
    row: dict[str, object] = {"ticker": ticker}
    cpath = chains / f"{ticker}.parquet"
    spath = spots / f"{ticker}.parquet"
    row["chain_file"] = cpath.exists()
    row["spot_file"] = spath.exists()
    if not cpath.exists():
        row["status"] = "MISSING chain"
        return row

    chain = pd.read_parquet(cpath, columns=["snapshot_date"])
    if chain.empty:
        row["status"] = "EMPTY chain"
        return row
    snap_dates = pd.to_datetime(chain["snapshot_date"]).dt.normalize().drop_duplicates()
    row["chain_rows"] = len(pd.read_parquet(cpath, columns=["strike"]))
    row["snap_days"] = len(snap_dates)
    row["date_min"] = snap_dates.min().date().isoformat()
    row["date_max"] = snap_dates.max().date().isoformat()

    # Contiguity: weekdays in-window with no snapshot (holidays inflate this a
    # little; the cross-ticker outliers are the real gaps).
    expected = _expected_trading_days()
    have = set(snap_dates)
    row["missing_weekdays"] = int(sum(1 for d in expected if d not in have))

    if not spath.exists():
        row["status"] = "MISSING spot"
        return row
    close = _daily_close(spath)
    if close.empty:
        row["status"] = "EMPTY spot"
        return row
    row["spot_days"] = len(close)

    # Panel-eligible obs: snapshot date is in the close index AND a +HOLD
    # forward close exists (the pre-reg's _build_panel drop rule, sans the
    # gamma/iv liquidity drop which needs the full chain — this is an UPPER
    # bound on usable n).
    idx = close.index
    eligible = 0
    for d in snap_dates:
        if d not in idx:
            continue
        pos = idx.get_indexer([d])[0]
        if pos >= 0 and pos + _HOLD < len(idx):
            eligible += 1
    row["panel_eligible"] = eligible
    row["status"] = "ok"
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="validate_fresh_window")
    ap.add_argument("--chains", type=Path, default=Path("data/chain_snapshots_2024"))
    ap.add_argument("--spots", type=Path, default=Path("data/spot_series_2024"))
    args = ap.parse_args(argv)

    print("=" * 86)
    print(f"FRESH-WINDOW DATA VALIDATION  chains={args.chains}  spots={args.spots}")
    print(f"window {_WIN_START}..{_WIN_END} · {len(_UNIVERSE)} tickers · ~{len(_expected_trading_days())} weekdays")
    print("=" * 86)

    if not args.chains.exists():
        print(f"!! chains dir does not exist: {args.chains}")
        return 1

    results = [_validate_ticker(t, args.chains, args.spots) for t in _UNIVERSE]
    hdr = f"{'ticker':<7}{'snap':>6}{'spot':>7}{'miss_wd':>8}{'elig':>6}  {'range':<24}{'status'}"
    print(hdr)
    print("-" * 86)
    bad: list[str] = []
    total_elig = 0
    for r in results:
        status = str(r.get("status", "?"))
        if status != "ok":
            bad.append(f"{r['ticker']} ({status})")
        rng = f"{r.get('date_min', '-')}..{r.get('date_max', '-')}"
        total_elig += int(r.get("panel_eligible", 0) or 0)
        print(f"{r['ticker']:<7}{r.get('snap_days', '-')!s:>6}{r.get('spot_days', '-')!s:>7}"
              f"{r.get('missing_weekdays', '-')!s:>8}{r.get('panel_eligible', '-')!s:>6}  "
              f"{rng:<24}{status}")
    print("-" * 86)
    print(f"total panel-eligible obs (upper bound, pre-conditioning): {total_elig}")
    if bad:
        print(f"\n!! BROKEN UNIVERSE — {len(bad)} ticker(s) not ok: {', '.join(bad)}")
        print("   Per the pre-reg, D must NOT run on a missing/empty universe.")
        return 1
    print("\nAll 24 tickers present and non-empty. Review missing_weekday outliers "
          "(heavy days dropped on JSONDecodeError) before trusting per-ticker n.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
