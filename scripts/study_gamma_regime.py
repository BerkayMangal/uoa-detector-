"""Research study: does dealer-gamma regime predict forward behaviour?

Tests the core GEX claim on the historical chain snapshots we already own
(2025-05..2026-04, ~251 days x 24 names). NOT production — a falsifiable
study run BEFORE building anything, per the project's test-before-believe
discipline.

Hypotheses
----------
H1 (vol):   short-gamma days (dealers net short gamma) have HIGHER forward
            realised volatility than long-gamma days (dealers amplify moves;
            long gamma suppresses them). Direction-agnostic, so market drift
            is irrelevant here.
H2 (drift): is there a directional edge? Measured MARKET-NEUTRAL by demeaning
            each day's forward return across the 24-name universe (the daily
            cross-sectional mean proxies "the market" — the same control that
            caught the drift illusion in the backtest).

Dealer GEX convention (SqueezeMetrics-style): net_gex = sum(call gamma*OI) -
sum(put gamma*OI). >0 = dealers long gamma (suppress); <0 = short (amplify).

Usage:  PYTHONPATH=src .venv/bin/python scripts/study_gamma_regime.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

_CHAINS = Path("data/chain_snapshots")
_SPOTS = Path("data/spot_series")
_NORM = 1.0 / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot: np.ndarray, strike: np.ndarray, t: np.ndarray, iv: np.ndarray) -> np.ndarray:
    """Black-Scholes gamma (r=0). Vectorised; safe on bad inputs."""
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_t = iv * np.sqrt(t)
        d1 = (np.log(spot / strike) + 0.5 * iv * iv * t) / vol_t
        pdf = _NORM * np.exp(-0.5 * d1 * d1)
        gamma = pdf / (spot * vol_t)
    return np.where(np.isfinite(gamma), gamma, 0.0)


def _net_gex_by_date(chain: pd.DataFrame) -> pd.Series:
    """Net dealer GEX per snapshot_date for one ticker."""
    df = chain.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["t"] = (df["expiry"] - df["snapshot_date"]).dt.days / 365.0
    for col in ("strike", "open_interest", "implied_volatility", "spot"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[(df["t"] > 0) & (df["implied_volatility"] > 0) & (df["open_interest"] > 0)]
    df = df[(df["strike"] > 0) & (df["spot"] > 0)].dropna(
        subset=["strike", "open_interest", "implied_volatility", "spot", "t"],
    )
    if df.empty:
        return pd.Series(dtype=float)
    gamma = _bs_gamma(
        df["spot"].to_numpy(), df["strike"].to_numpy(),
        df["t"].to_numpy(), df["implied_volatility"].to_numpy(),
    )
    sign = np.where(df["option_type"].str.lower().str.startswith("c"), 1.0, -1.0)
    df = df.assign(contrib=gamma * df["open_interest"].to_numpy() * sign)
    return df.groupby("snapshot_date")["contrib"].sum()


def _daily_returns(spot_path: Path) -> pd.DataFrame:
    """Daily close spot + forward returns + 5d forward realised vol."""
    s = pd.read_parquet(spot_path)
    s["minute"] = pd.to_datetime(s["minute"], utc=True).dt.tz_localize(None)
    s["spot"] = pd.to_numeric(s["spot"], errors="coerce")
    daily = s.dropna(subset=["spot"]).set_index("minute")["spot"].resample("1D").last().dropna()
    daily = daily[daily > 0]
    out = pd.DataFrame({"close": daily})
    out["r1"] = out["close"].shift(-1) / out["close"] - 1.0
    out["r5"] = out["close"].shift(-5) / out["close"] - 1.0
    dret = out["close"].pct_change()
    # forward 5-day realised vol = std of daily returns over [t+1 .. t+5]
    fwd = dret.shift(-1)
    out["fwd_vol5"] = fwd.rolling(5).std().shift(-4)
    out.index = out.index.normalize()
    return out


def _t_stat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(x.size)))


def main() -> int:
    rows: list[pd.DataFrame] = []
    tickers = sorted(p.stem for p in _CHAINS.glob("*.parquet"))
    for tk in tickers:
        spot_path = _SPOTS / f"{tk}.parquet"
        if not spot_path.exists():
            continue
        net = _net_gex_by_date(pd.read_parquet(_CHAINS / f"{tk}.parquet"))
        if net.empty:
            continue
        daily = _daily_returns(spot_path)
        net.index = pd.to_datetime(net.index).normalize()
        df = pd.DataFrame({"net_gex": net}).join(daily, how="inner")
        df["ticker"] = tk
        rows.append(df.reset_index(names="date"))

    data = pd.concat(rows, ignore_index=True).dropna(subset=["net_gex"])
    data["regime"] = np.where(data["net_gex"] < 0, "short", "long")
    # cross-sectional market-neutral forward returns (demean within each date)
    for col in ("r1", "r5"):
        data[f"{col}_mn"] = data[col] - data.groupby("date")[col].transform("mean")

    print(f"observations: {len(data)}  tickers: {data['ticker'].nunique()}  "
          f"dates: {data['date'].nunique()}")
    vc = data["regime"].value_counts()
    print(f"regime split:  short={vc.get('short', 0)}  long={vc.get('long', 0)}\n")

    short = data[data["regime"] == "short"]
    long_ = data[data["regime"] == "long"]

    print("=== H1: forward 5d realised vol by regime (short should be HIGHER) ===")
    sv, lv = short["fwd_vol5"].dropna(), long_["fwd_vol5"].dropna()
    print(f"  short-gamma mean vol: {sv.mean():.4f}   long-gamma mean vol: {lv.mean():.4f}")
    diff = sv.mean() - lv.mean()
    # Welch t for difference of means
    se = math.sqrt(sv.var(ddof=1) / sv.size + lv.var(ddof=1) / lv.size)
    print(f"  diff (short-long): {diff:+.4f}   t={diff / se:.2f}   "
          f"=> {'CONFIRMS' if diff > 0 and diff / se > 2 else 'no clear effect'}")
    # H1 honesty: the 5d realised-vol windows OVERLAP, so the full-sample t is
    # autocorrelation-inflated. Apply the SAME non-overlap gauntlet that killed
    # the directional claim (every 5th day per ticker = the 5d horizon).
    nonov = data.sort_values(["ticker", "date"]).groupby("ticker", group_keys=False).apply(
        lambda g: g.iloc[::5], include_groups=False)
    nsv = nonov[nonov["regime"] == "short"]["fwd_vol5"].dropna()
    nlv = nonov[nonov["regime"] == "long"]["fwd_vol5"].dropna()
    nse = math.sqrt(nsv.var(ddof=1) / nsv.size + nlv.var(ddof=1) / nlv.size)
    ndiff = nsv.mean() - nlv.mean()
    nt = ndiff / nse if nse else float("nan")
    print(f"  NON-OVERLAP (honest): diff={ndiff:+.4f}  t={nt:.2f}  n_short={nsv.size}  "
          f"=> {'survives' if abs(nt) > 2 else 'does NOT survive — the vol effect is NOT robust'}\n")

    print("=== H2: market-neutral forward return by regime (directional edge?) ===")
    for col, horizon in (("r1_mn", "1d"), ("r5_mn", "5d")):
        s_r = short[col].dropna().to_numpy()
        l_r = long_[col].dropna().to_numpy()
        print(f"  [{horizon}] short-gamma excess: {s_r.mean():+.4%} (t={_t_stat(s_r):.2f})   "
              f"long-gamma excess: {l_r.mean():+.4%} (t={_t_stat(l_r):.2f})")

    print("\n=== H2b: |market-neutral 5d move| by regime (vol, cross-sectional) ===")
    print(f"  short |r5_mn|: {short['r5_mn'].abs().mean():.4%}   "
          f"long |r5_mn|: {long_['r5_mn'].abs().mean():.4%}")

    print("\n=== dose-response: net_gex quintile -> fwd 5d realised vol ===")
    data["q"] = pd.qcut(data["net_gex"], 5, labels=["most-short", "2", "3", "4", "most-long"])
    print(data.groupby("q", observed=True)["fwd_vol5"].mean().to_string())

    _robustness(data)
    return 0


def _spread(df: pd.DataFrame) -> tuple[float, float, int]:
    """short-minus-long 5d market-neutral spread + t (Welch) + n_short."""
    s = df[df["regime"] == "short"]["r5_mn"].dropna().to_numpy()
    long_ = df[df["regime"] == "long"]["r5_mn"].dropna().to_numpy()
    if s.size < 2 or long_.size < 2:
        return (float("nan"), float("nan"), s.size)
    se = math.sqrt(s.var(ddof=1) / s.size + long_.var(ddof=1) / long_.size)
    diff = s.mean() - long_.mean()
    return (diff, diff / se if se else float("nan"), s.size)


def _robustness(data: pd.DataFrame) -> None:
    print("\n" + "=" * 64)
    print("ROBUSTNESS — is the directional edge real or a meme/window artifact?")
    print("=" * 64)

    # 1) Is short-gamma outperformance concentrated in a few names?
    print("\n[1] per-ticker: short-gamma 5d excess vs long, n_short (sorted)")
    rows = []
    for tk, g in data.groupby("ticker"):
        s = g[g["regime"] == "short"]["r5_mn"]
        long_ = g[g["regime"] == "long"]["r5_mn"]
        rows.append((tk, len(s), s.mean() if len(s) else float("nan"),
                     long_.mean() if len(long_) else float("nan")))
    rep = pd.DataFrame(rows, columns=["ticker", "n_short", "short_5d", "long_5d"])
    rep = rep.sort_values("n_short", ascending=False)
    print(rep.to_string(index=False, float_format=lambda x: f"{x:+.3%}"))
    pos = (rep["short_5d"] > 0).sum()
    print(f"  -> {pos}/{len(rep)} tickers have POSITIVE short-gamma 5d excess "
          "(broad = robust; concentrated = artifact)")

    # 2) Time split — does it hold in both halves?
    mid = data["date"].median()
    print(f"\n[2] time split at {pd.Timestamp(mid).date()} (short-long spread, t, n_short)")
    for name, part in (("first half", data[data["date"] <= mid]),
                       ("second half", data[data["date"] > mid])):
        d, t, n = _spread(part)
        print(f"  {name:12} spread={d:+.3%}  t={t:.2f}  n_short={n}")

    # 3) Non-overlapping 5d windows (every 5th date per ticker) — kills the
    #    overlap that inflates the t-stat.
    sub = data.sort_values(["ticker", "date"]).groupby("ticker", group_keys=False).apply(
        lambda g: g.iloc[::5], include_groups=False,
    )
    # rebuild ticker col (dropped by include_groups=False) is unnecessary here
    d, t, n = _spread(sub)
    print(f"\n[3] non-overlapping 5d  spread={d:+.3%}  t={t:.2f}  n_short={n}  (overlap removed)")

    # 4) Large-cap-only sanity (exclude the meme/high-beta cluster)
    memes = {"COIN", "MARA", "RIOT", "PLUG", "LCID", "RIVN", "HOOD", "SNAP",
             "AI", "PLTR", "DKNG", "RBLX", "CRWD", "ABNB"}
    large = data[~data["ticker"].isin(memes)]
    d, t, n = _spread(large)
    names = sorted(set(data["ticker"]) - memes)
    print(f"\n[4] large-caps only {names}: spread={d:+.3%}  t={t:.2f}  n_short={n}")


if __name__ == "__main__":
    raise SystemExit(main())
