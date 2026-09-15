"""Research study #2: is there a TRADEABLE gamma edge (pinning / reversion)?

Study #1 rejected the static regime->direction claim. This tests the mechanic
gamma traders actually use: in a LONG-gamma regime dealers suppress moves, so
price should oscillate between the put wall (support) and call wall
(resistance) and REVERT toward the centre; in SHORT gamma it should continue.

Signal: range_pos = (spot - put_wall) / (call_wall - put_wall), in [0,1].
  0 = at put wall (support), 1 = at call wall (resistance).
Hypothesis (long-gamma reversion): forward market-neutral return is NEGATIVELY
related to range_pos (near call wall -> fades; near put wall -> bounces).

Market-neutral via cross-sectional demeaning (the drift control). Robustness:
per-ticker, time-split, non-overlapping windows — the same gauntlet that
killed study #1's directional claim.

Usage:  PYTHONPATH=src:. .venv/bin/python scripts/study_gamma_pinning.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from webapp.gamma import compute_gamma

_CHAINS = Path("data/chain_snapshots")
_SPOTS = Path("data/spot_series")


def _daily(spot_path: Path) -> pd.DataFrame:
    s = pd.read_parquet(spot_path)
    s["minute"] = pd.to_datetime(s["minute"], utc=True).dt.tz_localize(None)
    s["spot"] = pd.to_numeric(s["spot"], errors="coerce")
    daily = s.dropna(subset=["spot"]).set_index("minute")["spot"].resample("1D").last().dropna()
    out = pd.DataFrame({"close": daily[daily > 0]})
    out["r5"] = out["close"].shift(-5) / out["close"] - 1.0
    out.index = out.index.normalize()
    return out


def _panel() -> pd.DataFrame:
    rows = []
    for path in sorted(_CHAINS.glob("*.parquet")):
        tk = path.stem
        sp = _SPOTS / f"{tk}.parquet"
        if not sp.exists():
            continue
        chain = pd.read_parquet(path)
        chain["snapshot_date"] = pd.to_datetime(chain["snapshot_date"])
        daily = _daily(sp)
        for date, snap in chain.groupby("snapshot_date"):
            m = compute_gamma(snap, as_of=str(pd.Timestamp(date).date()))
            if m is None or m["call_wall"] is None or m["put_wall"] is None:
                continue
            cw, pw, spot = m["call_wall"], m["put_wall"], m["spot"]
            if cw == pw:
                continue
            d = pd.Timestamp(date).normalize()
            if d not in daily.index:
                continue
            rows.append({
                "ticker": tk, "date": d, "net_gex": m["net_gex"], "spot": spot,
                "range_pos": (spot - pw) / (cw - pw), "r5": daily.loc[d, "r5"],
            })
    df = pd.DataFrame(rows).dropna(subset=["r5", "range_pos"])
    df["regime"] = np.where(df["net_gex"] < 0, "short", "long")
    df["r5_mn"] = df["r5"] - df.groupby("date")["r5"].transform("mean")
    # clip range_pos to a sane band (spot can sit outside the wall range)
    df = df[(df["range_pos"] > -1) & (df["range_pos"] < 2)]
    return df


def _corr_t(x: np.ndarray, y: np.ndarray) -> tuple[float, float, int]:
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    n = x.size
    if n < 10 or x.std() == 0 or y.std() == 0:
        return (float("nan"), float("nan"), n)
    r = float(np.corrcoef(x, y)[0, 1])
    t = r * math.sqrt((n - 2) / max(1e-9, 1 - r * r))
    return (r, t, n)


def _reversion_signal(df: pd.DataFrame) -> tuple[float, float, int]:
    """Bet toward the centre: signal = (0.5 - range_pos). Return signal-weighted
    mean forward return + t (the tradeable read)."""
    sig = (0.5 - df["range_pos"]).to_numpy()
    ret = df["r5_mn"].to_numpy()
    pnl = sig * ret  # long when below centre, short when above
    pnl = pnl[np.isfinite(pnl)]
    if pnl.size < 2 or pnl.std(ddof=1) == 0:
        return (float("nan"), float("nan"), pnl.size)
    return (float(pnl.mean()), float(pnl.mean() / (pnl.std(ddof=1) / math.sqrt(pnl.size))), pnl.size)


def main() -> int:
    df = _panel()
    print(f"panel: {len(df)} obs  tickers={df['ticker'].nunique()}  dates={df['date'].nunique()}")
    print(f"regime: long={ (df['regime']=='long').sum() }  short={ (df['regime']=='short').sum() }\n")

    for regime in ("long", "short"):
        g = df[df["regime"] == regime]
        r, t, n = _corr_t(g["range_pos"].to_numpy(), g["r5_mn"].to_numpy())
        print(f"[{regime:5} gamma] corr(range_pos, fwd5d_mn) = {r:+.3f}  t={t:.2f}  n={n}  "
              f"({'reversion' if r < 0 else 'continuation'})")
        # tercile means
        g = g.assign(b=pd.qcut(g["range_pos"], 3, labels=["near put wall", "mid", "near call wall"],
                               duplicates="drop"))
        print("   fwd5d_mn by position:")
        print(g.groupby("b", observed=True)["r5_mn"].mean().to_string().replace("\n", "\n   "))

    print("\n=== TRADEABLE: long-gamma reversion-to-centre signal ===")
    longg = df[df["regime"] == "long"]
    m, t, n = _reversion_signal(longg)
    print(f"  signal-weighted mean fwd5d_mn = {m:+.4%}  t={t:.2f}  n={n}")

    print("\n  robustness (long-gamma reversion):")
    mid = df["date"].median()
    for name, part in (("first half", longg[longg["date"] <= mid]),
                       ("second half", longg[longg["date"] > mid])):
        m, t, n = _reversion_signal(part)
        print(f"   {name:12} mean={m:+.4%}  t={t:.2f}  n={n}")
    nonov = longg.sort_values(["ticker", "date"]).groupby("ticker", group_keys=False).apply(
        lambda x: x.iloc[::5], include_groups=False,
    )
    m, t, n = _reversion_signal(nonov)
    print(f"   non-overlap  mean={m:+.4%}  t={t:.2f}  n={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
