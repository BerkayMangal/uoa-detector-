"""Research study #3: vol-risk-premium / vol-timing — the one surviving lead.

Study #1 & #2 rejected directional + pinning gamma alpha. The ONE thing that
survived is the vol effect (extreme gamma -> higher realised vol). Its
tradeable form is not direction but VOL: is implied vol (what you pay) cheap or
rich vs the realised vol that follows — and can gamma / IV-level time it?

For each ticker-day:
  implied  = ATM IV (annualised vol the option market is charging)
  realised = annualised realised vol of the underlying over the NEXT 20 days
  vrp      = implied - realised   (>0 options rich = sell-vol won;
                                    <0 options cheap = buy-vol won)

Tests:
  1. Mean VRP > 0 ? (does the vol risk premium exist here)
  2. Does gamma regime predict VRP sign? (short/extreme gamma -> realised
     overshoots implied -> buying vol/options has edge)
  3. IV-level mean-reversion: low IV -> buy, high IV -> sell?
Robustness: time-split + non-overlap, the gauntlet that killed #1 and #2.

Usage:  PYTHONPATH=src:. .venv/bin/python scripts/study_vol_premium.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from webapp.gamma import compute_gamma

_CHAINS = Path("data/chain_snapshots")
_SPOTS = Path("data/spot_series")
_ANN = math.sqrt(252.0)


def _daily_close(spot_path: Path) -> pd.Series:
    s = pd.read_parquet(spot_path)
    s["minute"] = pd.to_datetime(s["minute"], utc=True).dt.tz_localize(None)
    s["spot"] = pd.to_numeric(s["spot"], errors="coerce")
    daily = s.dropna(subset=["spot"]).set_index("minute")["spot"].resample("1D").last().dropna()
    daily.index = daily.index.normalize()
    return daily[daily > 0]


def _atm_iv(snap: pd.DataFrame, spot: float) -> float | None:
    df = snap.copy()
    for c in ("strike", "implied_volatility"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["dte"] = (df["expiry"] - df["snapshot_date"]).dt.days
    df = df[(df["dte"] >= 10) & (df["dte"] <= 45) & (df["implied_volatility"] > 0)]
    df = df[(df["strike"] - spot).abs() <= 0.05 * spot]
    return float(df["implied_volatility"].median()) if not df.empty else None


def _realised_fwd(close: pd.Series, d: pd.Timestamp, n: int = 20) -> float | None:
    fut = close[close.index > d].head(n)
    if len(fut) < n:
        return None
    rets = np.log(fut.to_numpy()[1:] / fut.to_numpy()[:-1])
    return float(rets.std(ddof=1) * _ANN) if rets.size > 1 else None


def _panel() -> pd.DataFrame:
    rows = []
    for path in sorted(_CHAINS.glob("*.parquet")):
        tk = path.stem
        sp = _SPOTS / f"{tk}.parquet"
        if not sp.exists():
            continue
        chain = pd.read_parquet(path)
        chain["snapshot_date"] = pd.to_datetime(chain["snapshot_date"])
        close = _daily_close(sp)
        for date, snap in chain.groupby("snapshot_date"):
            d = pd.Timestamp(date).normalize()
            m = compute_gamma(snap, as_of=str(d.date()))
            if m is None:
                continue
            iv = _atm_iv(snap, m["spot"])
            rv = _realised_fwd(close, d)
            if iv is None or rv is None:
                continue
            rows.append({"ticker": tk, "date": d, "net_gex": m["net_gex"],
                         "implied": iv, "realised": rv, "vrp": iv - rv})
    df = pd.DataFrame(rows)
    df["regime"] = np.where(df["net_gex"] < 0, "short", "long")
    # IV percentile within each ticker's own history
    df["iv_pct"] = df.groupby("ticker")["implied"].rank(pct=True)
    return df


def _t(x: np.ndarray) -> tuple[float, float, int]:
    x = x[np.isfinite(x)]
    if x.size < 2 or x.std(ddof=1) == 0:
        return (float("nan"), float("nan"), x.size)
    return (float(x.mean()), float(x.mean() / (x.std(ddof=1) / math.sqrt(x.size))), x.size)


def main() -> int:
    df = _panel()
    print(f"panel: {len(df)} obs  tickers={df['ticker'].nunique()}  dates={df['date'].nunique()}")
    m, t, n = _t(df["vrp"].to_numpy())
    print(f"\n[1] mean VRP (implied-realised, annualised) = {m:+.4f}  t={t:.2f}  "
          f"({'options RICH (sell-vol premium exists)' if m > 0 else 'options cheap'})")

    print("\n[2] VRP by gamma regime (short/extreme gamma -> options cheap?)")
    for r in ("long", "short"):
        m, t, n = _t(df[df["regime"] == r]["vrp"].to_numpy())
        print(f"   {r:5} gamma: mean VRP={m:+.4f}  t={t:.2f}  n={n}  "
              f"-> {'BUY vol (realised overshoots)' if m < 0 else 'sell vol'}")

    print("\n[3] VRP by IV percentile (low IV cheap -> buy? high IV rich -> sell?)")
    df["ivb"] = pd.qcut(df["iv_pct"], 4, labels=["low IV", "2", "3", "high IV"])
    print(df.groupby("ivb", observed=True)["vrp"].mean().to_string().replace("\n", "\n   "))

    mid = df["date"].median()

    def _robust(sel: pd.DataFrame, pnl_sign: float, label: str) -> None:
        # pnl_sign +1 = short vol (profit=vrp); -1 = long vol (profit=-vrp)
        m, t, n = _t((pnl_sign * sel["vrp"]).to_numpy())
        print(f"  {label}: mean={m:+.4f}  t={t:.2f}  n={n}")
        for nm, part in (("first half", sel[sel["date"] <= mid]),
                         ("second half", sel[sel["date"] > mid])):
            m, t, n = _t((pnl_sign * part["vrp"]).to_numpy())
            print(f"     {nm:12} mean={m:+.4f}  t={t:.2f}  n={n}")
        nonov = sel.sort_values(["ticker", "date"]).groupby("ticker", group_keys=False).apply(
            lambda x: x.iloc[::20], include_groups=False,
        )
        m, t, n = _t((pnl_sign * nonov["vrp"]).to_numpy())
        print(f"     non-overlap  mean={m:+.4f}  t={t:.2f}  n={n}  <- the honest one")

    print("\n=== TRADEABLE A: SELL vol when long-gamma & high-IV (the strong side) ===")
    sellvol = df[(df["regime"] == "long") & (df["iv_pct"] >= 0.75)]
    _robust(sellvol, +1.0, "short-vol selection (profit = implied-realised)")

    print("\n=== TRADEABLE B: BUY vol when short-gamma & low-IV ===")
    buyvol = df[(df["regime"] == "short") & (df["iv_pct"] <= 0.25)]
    _robust(buyvol, -1.0, "long-vol selection (profit = realised-implied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
