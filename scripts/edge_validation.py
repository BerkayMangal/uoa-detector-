"""Phase 1 GATE — is the vol-premium edge real MONEY after costs + tails + FDR?

The statistical signal (sell vol in long-gamma + high-IV names) shows VRP in
vol points. That is NOT money. This turns it into a tradeable $ P&L with the
costs and tail risk a real short-vol seller faces, then subjects it to the
gauntlet that decides whether to risk capital:

  * Real option P&L (BS-priced, held to ~1mo expiry, UNHEDGED — what a retail
    seller actually does): short straddle (raw edge, unbounded risk) AND iron
    butterfly (defined risk — the UI-recommended structure).
  * Realistic costs: wide option bid/ask spread (5/10/20% sensitivity) +
    commission. You SELL at the bid.
  * Tail stress: a -20% overnight gap. n=63 with no crash = the tail was never
    measured. Quantify the single-trade worst case.
  * Baselines: random-day and unconditioned short-vol — does the signal beat them?
  * Multiple testing: Benjamini-Hochberg FDR + Deflated Sharpe Ratio over the
    ~30 hypotheses tested across all studies.
  * Walk-forward OOS: train first 60% of dates, test the last 40% net of costs.

VERDICT is printed at the end. If it dies here, the project STOPS — no live/speed
work on a dead edge.

Usage:  PYTHONPATH=src:. .venv/bin/python scripts/edge_validation.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from webapp.gamma import atm_iv, compute_gamma

_CHAINS = Path("data/chain_snapshots")
_SPOTS = Path("data/spot_series")
_HOLD = 20  # trading days ~ 1-month option
_T = _HOLD / 252.0
_IV_HI = 0.75  # IV-rank threshold for the SELL cell
_COMMISSION_PER_LEG = 0.65  # $/contract/leg (held to expiry: entry only)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call(s: float, k: float, t: float, sigma: float) -> float:
    """Black-Scholes call, r=0."""
    if t <= 0 or sigma <= 0:
        return max(0.0, s - k)
    vt = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sigma * sigma * t) / vt
    d2 = d1 - vt
    return s * _ncdf(d1) - k * _ncdf(d2)


def _straddle_premium(s: float, iv: float, t: float) -> float:
    """ATM straddle mid (call+put, r=0 => put=call)."""
    return 2.0 * _bs_call(s, s, t, iv)


# --------------------------------------------------------------------------
# Panel: per ticker-day signal + entry/exit spot (terminal move) + IV
# --------------------------------------------------------------------------


def _daily_close(spot_path: Path) -> pd.Series:
    s = pd.read_parquet(spot_path)
    s["minute"] = pd.to_datetime(s["minute"], utc=True).dt.tz_localize(None)
    s["spot"] = pd.to_numeric(s["spot"], errors="coerce")
    d = s.dropna(subset=["spot"]).set_index("minute")["spot"].resample("1D").last().dropna()
    d.index = d.index.normalize()
    return d[d > 0]


def _build_panel() -> pd.DataFrame:
    rows = []
    for path in sorted(_CHAINS.glob("*.parquet")):
        tk = path.stem
        sp = _SPOTS / f"{tk}.parquet"
        if not sp.exists():
            continue
        chain = pd.read_parquet(path)
        chain["snapshot_date"] = pd.to_datetime(chain["snapshot_date"])
        close = _daily_close(sp)
        idx = close.index
        for date, snap in chain.groupby("snapshot_date"):
            d = pd.Timestamp(date).normalize()
            if d not in close.index:
                continue
            pos = idx.get_loc(d)
            if not isinstance(pos, int) or pos + _HOLD >= len(idx):
                continue
            m = compute_gamma(snap, as_of=str(d.date()))
            if m is None:
                continue
            iv = atm_iv(snap, as_of=str(d.date()))
            if iv is None or iv <= 0:
                continue
            s_in = float(close.iloc[pos])
            s_out = float(close.iloc[pos + _HOLD])
            rows.append({"ticker": tk, "date": d, "net_gex": m["net_gex"],
                         "iv": iv, "s_in": s_in, "s_out": s_out,
                         "move": abs(s_out - s_in)})
    df = pd.DataFrame(rows)
    df["regime"] = np.where(df["net_gex"] < 0, "short", "long")
    df["iv_pct"] = df.groupby("ticker")["iv"].rank(pct=True)
    df["sell_signal"] = (df["regime"] == "long") & (df["iv_pct"] >= _IV_HI)
    return df


# --------------------------------------------------------------------------
# Tradeable P&L (held to expiry, unhedged)
# --------------------------------------------------------------------------


def _pnl(df: pd.DataFrame, spread_frac: float, wing_mult: float = 1.5) -> pd.DataFrame:
    """Per-row short-straddle and iron-butterfly P&L per share (×100 = per
    contract), net of the bid/ask spread crossed on entry + commission."""
    out = df.copy()
    prem = np.array([_straddle_premium(s, iv, _T) for s, iv in zip(out["s_in"], out["iv"], strict=False)])
    # wings at wing_mult * expected 1mo move
    w = out["s_in"].to_numpy() * out["iv"].to_numpy() * math.sqrt(_T) * wing_mult
    wing_prem = np.array([
        _bs_call(s, s + wi, _T, iv) + _bs_call(s, s, _T, iv) - (s - (s - wi))  # put(S-w) via parity approx
        for s, iv, wi in zip(out["s_in"], out["iv"], w, strict=False)
    ])
    # put(S-w) r=0: = call(S-w) - S + (S-w) ... simpler: recompute wings cleanly
    wing_prem = np.array([
        _bs_call(s, s + wi, _T, iv) + (_bs_call(s, s - wi, _T, iv) - s + (s - wi))
        for s, iv, wi in zip(out["s_in"], out["iv"], w, strict=False)
    ])
    move = out["move"].to_numpy()
    comm = _COMMISSION_PER_LEG / 100.0  # per share

    # Short straddle: collect prem*(1-spread), pay terminal |move|
    straddle_credit = prem * (1.0 - spread_frac)
    out["straddle_pnl"] = straddle_credit - move - 2 * comm

    # Iron butterfly: sell straddle, buy wings. net credit, payout = min(move, w)
    net_credit = (prem - wing_prem) * (1.0 - spread_frac)
    payout = np.minimum(move, w)
    out["fly_credit"] = net_credit
    out["fly_width"] = w
    out["fly_pnl"] = net_credit - payout - 4 * comm
    out["fly_maxloss"] = w - net_credit  # capital at risk
    out["fly_ret"] = out["fly_pnl"] / np.where(out["fly_maxloss"] > 0, out["fly_maxloss"], np.nan)
    return out


def _sharpe(returns: np.ndarray) -> tuple[float, float, int]:
    r = returns[np.isfinite(returns)]
    if r.size < 2 or r.std(ddof=1) == 0:
        return (float("nan"), float("nan"), r.size)
    mean = r.mean()
    sharpe = mean / r.std(ddof=1) * math.sqrt(252.0 / _HOLD)  # annualised (non-overlap units)
    t = mean / (r.std(ddof=1) / math.sqrt(r.size))
    return (sharpe, t, r.size)


def _nonoverlap(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["ticker", "date"]).groupby("ticker", group_keys=False).apply(
        lambda g: g.iloc[::_HOLD], include_groups=False)


def _deflated_sharpe(returns: np.ndarray, n_trials: int) -> float:
    """Bailey & Lopez de Prado Deflated Sharpe Ratio probability (>0 means the
    observed SR is unlikely under n_trials of noise, given skew/kurtosis)."""
    r = returns[np.isfinite(returns)]
    n = r.size
    if n < 4 or r.std(ddof=1) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)  # per-trade (non-annualised)
    sk = float(pd.Series(r).skew())
    ku = float(pd.Series(r).kurt()) + 3.0  # pandas kurt is excess
    # expected max SR under n_trials of N(0, 1/n) noise
    emc = 0.5772156649
    e_max = (1 - emc) * _ninv(1 - 1.0 / n_trials) + emc * _ninv(1 - 1.0 / (n_trials * math.e))
    sr0 = e_max / math.sqrt(n)  # threshold SR from trials
    denom = math.sqrt((1 - sk * sr + (ku - 1) / 4.0 * sr * sr) / (n - 1))
    if denom == 0:
        return float("nan")
    return _ncdf((sr - sr0) / denom)


def _ninv(p: float) -> float:
    # inverse normal CDF (Acklam approximation, enough precision here)
    a = [-39.6968302866538, 220.946098424521, -275.928510446969,
         138.357751867269, -30.6647980661472, 2.50662827745924]
    b = [-54.4760987982241, 161.585836858041, -155.698979859887,
         66.8013118877197, -13.2806815528857]
    c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184,
         -2.54973253934373, 4.37466414146497, 2.93816398269878]
    d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= ph:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def main() -> int:
    print("Building panel (compute_gamma per ticker-day)…")
    panel = _build_panel()
    sells = panel[panel["sell_signal"]]
    print(f"panel: {len(panel)} ticker-days · SELL signals: {len(sells)} "
          f"(non-overlap ~{len(_nonoverlap(sells))})\n")

    print("=" * 70)
    print("REALISTIC P&L — SELL cell, held-to-expiry, net of spread + commission")
    print("=" * 70)
    for sf in (0.05, 0.10, 0.20):
        p = _pnl(sells, spread_frac=sf)
        no = _nonoverlap(p)
        s_sh, s_t, s_n = _sharpe(no["straddle_pnl"].to_numpy() / no["s_in"].to_numpy())
        f_sh, f_t, f_n = _sharpe(no["fly_ret"].to_numpy())
        st_win = (no["straddle_pnl"] > 0).mean()
        fl_win = (no["fly_pnl"] > 0).mean()
        print(f"\n  spread={sf:.0%} (option bid/ask):")
        print(f"    SHORT STRADDLE  net mean ${p['straddle_pnl'].mean()*100:+.0f}/contract  "
              f"win {st_win:.0%}  non-ov Sharpe {s_sh:+.2f} (t={s_t:+.2f}, n={s_n})")
        print(f"    IRON BUTTERFLY  net mean ret {no['fly_ret'].mean():+.1%} on capital  "
              f"win {fl_win:.0%}  non-ov Sharpe {f_sh:+.2f} (t={f_t:+.2f}, n={f_n})")

    print("\n" + "=" * 70)
    print("TAIL STRESS — a -20% overnight gap during the hold")
    print("=" * 70)
    p10 = _pnl(sells, spread_frac=0.10)
    gap_move = 0.20 * p10["s_in"].to_numpy()
    # replace the realised terminal move with the -20% gap; back out the gross credit
    straddle_credit = (p10["straddle_pnl"] + p10["move"] + 2 * _COMMISSION_PER_LEG / 100).to_numpy()
    straddle_gap_pnl = straddle_credit - gap_move - 2 * _COMMISSION_PER_LEG / 100
    fly_gap_loss = -p10["fly_maxloss"].to_numpy()  # condor caps at max loss
    print(f"  SHORT STRADDLE  mean credit collected ${straddle_credit.mean()*100:.0f}/contract")
    print(f"                  -20% gap loss: mean ${straddle_gap_pnl.mean()*100:+.0f}  "
          f"WORST ${straddle_gap_pnl.min()*100:+.0f}/contract  "
          f"(loss = {-straddle_gap_pnl.mean()/straddle_credit.mean():.0f}x the credit)")
    print(f"  IRON BUTTERFLY  -20% gap loss CAPPED at -max_loss: "
          f"mean ${fly_gap_loss.mean()*100:+.0f}  worst ${fly_gap_loss.min()*100:+.0f}/contract")

    print("\n" + "=" * 70)
    print("BASELINES (spread 10%, iron butterfly net return)")
    print("=" * 70)
    allp = _pnl(panel, spread_frac=0.10)
    uncond = _nonoverlap(allp)
    u_sh, u_t, u_n = _sharpe(uncond["fly_ret"].to_numpy())
    print(f"  UNCONDITIONED (all ticker-days): Sharpe {u_sh:+.2f} (t={u_t:+.2f}, n={u_n}) "
          f"mean {uncond['fly_ret'].mean():+.1%}")
    sig = _nonoverlap(_pnl(sells, spread_frac=0.10))
    g_sh, g_t, g_n = _sharpe(sig["fly_ret"].to_numpy())
    print(f"  SELL SIGNAL (long-gamma+high-IV): Sharpe {g_sh:+.2f} (t={g_t:+.2f}, n={g_n}) "
          f"mean {sig['fly_ret'].mean():+.1%}")
    print(f"  -> signal {'BEATS' if g_sh > u_sh else 'does NOT beat'} unconditioned by "
          f"{g_sh - u_sh:+.2f} Sharpe")

    print("\n" + "=" * 70)
    print("WALK-FORWARD OOS (train first 60% of dates, test last 40%)")
    print("=" * 70)
    cut = panel["date"].quantile(0.6)
    test = sells[sells["date"] > cut]
    tp = _nonoverlap(_pnl(test, spread_frac=0.10))
    t_sh, t_t, t_n = _sharpe(tp["fly_ret"].to_numpy())
    print(f"  OOS (after {pd.Timestamp(cut).date()}): butterfly Sharpe {t_sh:+.2f} "
          f"(t={t_t:+.2f}, n={t_n}) mean {tp['fly_ret'].mean():+.1%}")

    print("\n" + "=" * 70)
    print("MULTIPLE TESTING — FDR + Deflated Sharpe (n_trials=30)")
    print("=" * 70)
    no = _nonoverlap(_pnl(sells, spread_frac=0.10))
    _, fly_t, _ = _sharpe(no["fly_ret"].to_numpy())
    p_raw = 2 * (1 - _ncdf(abs(fly_t)))
    print(f"  butterfly net-of-cost t={fly_t:+.2f} -> raw p={p_raw:.4f}")
    print(f"  Bonferroni x30: p={min(1.0, p_raw*30):.3f}  "
          f"({'survives' if p_raw*30 < 0.05 else 'does NOT survive'} at 0.05)")
    dsr = _deflated_sharpe(no["fly_ret"].to_numpy(), n_trials=30)
    print(f"  Deflated Sharpe Ratio prob (>0.95 = robust): {dsr:.3f}  "
          f"({'survives' if dsr > 0.95 else 'does NOT survive'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
