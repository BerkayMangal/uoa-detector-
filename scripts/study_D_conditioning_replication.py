"""Study D — does the conditioning signal replicate on a FRESH window?

Pre-registered in docs/preregister_D.md (committed before this script ran). This
script does NOT define the test; it executes the frozen design. It is parameterised
by data directory so the SAME code runs on:

  * the burned panel  -> INSTRUMENT CHECK only (reproduce the known in-sample
    signal; verify the wiring). NOT the D verdict.
  * a fresh, non-overlapping window (2024-05-01..2025-04-30, per the pre-reg) ->
    the real single run, once the data exists.

Conditioning is inherited VERBATIM from scripts/edge_validation.py (_build_panel:
net_gex sign from webapp.gamma.compute_gamma, atm_iv, sell_signal = long-gamma &
iv_pct>=0.75). Nothing is re-tuned here. The statistic is the pure-information
vol-points VRP Sharpe-spread (COND - UNCOND): no option priced, no cost, no tail.

Usage (instrument check on burned panel — explicitly labelled, not the verdict):
    PYTHONPATH=src:. .venv/bin/python scripts/study_D_conditioning_replication.py \
        --chains data/chain_snapshots --spots data/spot_series --mode instrument-check

Usage (the real run, once fresh data is downloaded per the pre-reg):
    PYTHONPATH=src:. .venv/bin/python scripts/study_D_conditioning_replication.py \
        --chains data/chain_snapshots_2024 --spots data/spot_series_2024 --mode fresh-run
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import edge_validation as ev  # inherit the EXACT conditioning + panel + stats
import numpy as np
import pandas as pd


def _vrp(panel: pd.DataFrame) -> pd.DataFrame:
    """Per-obs pure-information VRP in vol-points/$spot (pre-reg §4).

    implied_move = s_in * iv * sqrt(T);  realized_move = |s_out - s_in| (= panel
    'move');  vrp = (implied_move - realized_move)/s_in.  >0 = vol overpriced.
    No option priced, no cost, no tail.
    """
    out = panel.copy()
    implied_move = out["s_in"].to_numpy() * out["iv"].to_numpy() * math.sqrt(ev._T)
    out["vrp"] = (implied_move - out["move"].to_numpy()) / out["s_in"].to_numpy()
    return out


def _causal_iv_pct(panel: pd.DataFrame) -> pd.Series:
    """Secondary (pre-reg §2): trailing expanding per-ticker IV percentile — uses
    only prior+current observations, no full-window look-ahead."""
    p = panel.sort_values(["ticker", "date"])
    return p.groupby("ticker")["iv"].transform(
        lambda s: s.expanding().apply(lambda w: float((w <= w.iloc[-1]).mean()), raw=False)
    )


def _dsr(returns: np.ndarray, n_trials: int) -> float:
    """Deflated Sharpe. For n_trials>=2 defer to the inherited edge_validation
    implementation. For n_trials==1 (a single pre-registered hypothesis) the
    expected max over one trial is 0, so the deflation benchmark SR0=0 and DSR
    reduces to the Probabilistic Sharpe Ratio vs 0 — computed here with the SAME
    skew/kurtosis/length adjustment, avoiding the _ninv(0) singularity."""
    if n_trials >= 2:
        return ev._deflated_sharpe(returns, n_trials)
    r = returns[np.isfinite(returns)]
    n = r.size
    if n < 4 or r.std(ddof=1) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    sk = float(pd.Series(r).skew())
    ku = float(pd.Series(r).kurt()) + 3.0
    denom = math.sqrt((1 - sk * sr + (ku - 1) / 4.0 * sr * sr) / (n - 1))
    return float("nan") if denom == 0 else ev._ncdf(sr / denom)


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if a.size < 2 or b.size < 2:
        return float("nan")
    va, vb = a.var(ddof=1), b.var(ddof=1)
    denom = math.sqrt(va / a.size + vb / b.size)
    return float("nan") if denom == 0 else (a.mean() - b.mean()) / denom


def _arm_report(no: pd.DataFrame, mask: pd.Series, label: str) -> tuple[float, float, int]:
    sh, t, n = ev._sharpe(no.loc[mask, "vrp"].to_numpy())
    print(f"    {label:<28} Sharpe {sh:+.2f}  (t={t:+.2f}, mean_vrp={no.loc[mask, 'vrp'].mean():+.4f}, n={n})")
    return sh, t, n


def _run_one(panel: pd.DataFrame, sig_col: str, title: str) -> None:
    no = ev._nonoverlap(panel)
    cond = no[sig_col].astype(bool)
    print(f"\n  {title}")
    u_sh, _u_t, _u_n = _arm_report(no, pd.Series(True, index=no.index), "UNCOND (blind, all obs)")
    c_sh, c_t, _c_n = _arm_report(no, cond, "COND (long-gamma+high-IV)")
    spread = c_sh - u_sh
    # IC = point-biserial corr(signal, vrp) over all non-overlap obs
    sig = no[sig_col].astype(float).to_numpy()
    vrp = no["vrp"].to_numpy()
    fin = np.isfinite(sig) & np.isfinite(vrp)
    ic = float(np.corrcoef(sig[fin], vrp[fin])[0, 1]) if fin.sum() > 2 and sig[fin].std() > 0 else float("nan")
    welch = _welch_t(no.loc[cond, "vrp"].to_numpy(), no.loc[~cond, "vrp"].to_numpy())
    dsr = _dsr(no.loc[cond, "vrp"].to_numpy(), n_trials=1)
    print(f"    -> SPREAD (COND-UNCOND) = {spread:+.2f} Sharpe   IC={ic:+.3f}   "
          f"t_cond={c_t:+.2f}   Welch_t(cond-uncond)={welch:+.2f}   DSR(n_trials=1)={dsr:.3f}")
    # pre-registered verdict logic (pre-reg §6) — printed for transparency
    if spread <= 0:
        verdict = "DEAD (artifact) -> B/C dead-on-arrival"
    elif abs(c_t) < 2 or not (dsr > 0.95):
        verdict = "WEAK -> no green light to B/C"
    else:
        verdict = "SURVIVED -> design C together (do not start)"
    print(f"    -> pre-registered verdict mapping: {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="data/chain_snapshots")
    ap.add_argument("--spots", default="data/spot_series")
    ap.add_argument("--mode", choices=["instrument-check", "fresh-run"], required=True)
    args = ap.parse_args()

    ev._CHAINS = Path(args.chains)
    ev._SPOTS = Path(args.spots)

    print("=" * 78)
    if args.mode == "instrument-check":
        print("STUDY D — INSTRUMENT CHECK (burned/in-sample data). NOT the D verdict.")
        print("Purpose: verify the wiring and reproduce the known in-sample signal so")
        print("the script is trusted before spending a fresh download. No inference re D.")
    else:
        print("STUDY D — FRESH-WINDOW RUN (the real, single, pre-registered test).")
    print(f"data: chains={args.chains}  spots={args.spots}")
    print("=" * 78)

    panel = _vrp(ev._build_panel())
    panel["sell_signal_causal"] = (
        (panel["regime"] == "long") & (_causal_iv_pct(panel) >= ev._IV_HI)
    )
    print(f"panel: {len(panel)} valid ticker-days · "
          f"SELL(primary) {int(panel['sell_signal'].sum())} · "
          f"SELL(causal) {int(panel['sell_signal_causal'].sum())}")

    _run_one(panel, "sell_signal", "PRIMARY (inherited iv_pct, full-window rank)")
    _run_one(panel, "sell_signal_causal", "SECONDARY (causal trailing iv_pct, look-ahead-free)")

    if args.mode == "instrument-check":
        print("\n[instrument-check] The numbers above are IN-SAMPLE on the burned panel.")
        print("They are NOT evidence for or against D. D requires the fresh window")
        print("(2024-05-01..2025-04-30) per docs/preregister_D.md — not yet downloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
