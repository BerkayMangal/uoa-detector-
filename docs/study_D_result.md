# Study D — Result

**Pre-registered:** `docs/preregister_D.md` (commit `2e3094a`, before any computation).

## Top-line verdict: NOT YET TESTABLE — execution BLOCKED on fresh data

**D was not run on a fresh window, because no fresh data exists locally.** Per the
pre-registered rule "if there isn't enough fresh, non-overlapping data, STOP — do not
proceed on a bad window," the verdict is **withheld**. D is neither SURVIVED nor DEAD.
**B and C remain GATED — they are neither cleared nor killed.** Nothing is started.

This is the honest outcome, not a failure: the design is frozen and the instrument is
verified, so the moment a fresh window is downloaded the test produces a clean,
single-shot answer with zero researcher degrees of freedom.

---

## Why blocked — the data wall (evidence)

All local option data ends **2026-04-30** and lies entirely inside the burned panel:

| Source | Coverage | Overlap with burned panel |
|---|---|---|
| `data/chain_snapshots` (the panel) | 2025-05-01 → 2026-04-30, 24 tickers, ~8.0M rows | IS the burned panel |
| `data/historical/bulk/<T>/YYYY-MM` | 2025-05 → 2026-04 (raw source of the panel) | full overlap |
| `data/historical/dry-run*` | 2026-04 only | full overlap |

There is **zero** local data before 2025-05 or after 2026-04. A clean replication is
therefore impossible without a new download. The forward window (2026-05-01 → today)
is only ~7 weeks → ~1 non-overlap obs/ticker → underpowered, disqualified (pre-reg §1).

---

## Instrument check (IN-SAMPLE, burned panel) — NOT the D verdict

To verify the script is correctly wired *before* anyone spends a fresh download, the
exact study code was run **once** on the burned panel. This reproduces a *known*
in-sample result; it draws **no inference about D** (D is out-of-sample by definition).

```
panel: 5342 valid ticker-days · SELL(primary)=1082 · SELL(causal)=1442

PRIMARY (inherited iv_pct, full-window rank):
  UNCOND (blind, all obs)    Sharpe +1.19  (t=+5.57, n=278)
  COND  (long-gamma+high-IV) Sharpe +1.99  (t=+4.62, n=68)
  SPREAD = +0.80   IC=+0.122   t_cond=+4.62   Welch_t=+2.06   DSR(n_trials=1)=1.000

SECONDARY (causal trailing iv_pct, look-ahead-free):
  COND  Sharpe +1.70 (t=+4.76, n=99)
  SPREAD = +0.51   IC=+0.113   t_cond=+4.76   Welch_t=+1.88   DSR=1.000
```

**What this does and does not establish:**

- ✅ **The instrument is correct.** The pure-information VRP Sharpe-spread **+0.80**
  reproduces the documented **+0.77** fly-return spread almost exactly — the same
  conditioning, computed independently, recovers the same in-sample signal. The script
  is trustworthy for the fresh run.
- ✅ **The look-ahead bracket works.** Stripping the inherited full-window IV-rank
  look-ahead (causal variant) shrinks the spread **+0.80 → +0.51** and the cond-vs-
  uncond Welch t **+2.06 → +1.88** (drops below 2). So a meaningful slice of the
  in-sample spread is look-ahead; the causal number is the more honest one to expect.
- ❌ **This is NOT evidence the conditioning replicates.** It is in-sample on the
  burned panel — the conditioning was *defined* on this data, so detecting it here is
  circular by construction. Replication = the same spread surviving on the **untouched
  2024 window**, which has not been run.

---

## Deviation log (pre-reg §6 / CLAUDE.md D1)

- **DSR at `n_trials=1`.** The inherited `edge_validation._deflated_sharpe` divides by
  `log(1 − 1/n_trials)` and hits a `log(0)` singularity at `n_trials=1`. Mathematically
  the expected max over a single trial is 0, so the deflation benchmark `SR0=0` and DSR
  reduces to the Probabilistic Sharpe Ratio vs 0. Implemented as `_dsr()` in the study
  script with the **same** skew/kurtosis/length adjustment; `edge_validation.py` (the
  frozen artifact) was **not** modified. This is the faithful realization of
  "DSR n_trials=1," not a design change.

---

## What is needed to run D (the real test)

Download the pre-registered fresh window (operator's job — historical ThetaData pull,
CLAUDE.md):

- **Window:** 2024-05-01 → 2025-04-30 · **Tickers:** the same 24 (pre-reg §1).
- **Into:** `data/chain_snapshots_2024/` + `data/spot_series_2024/` (NOT the burned dirs).
- **Pipeline:** `scripts/download_chain_snapshots.py` → `scripts/compute_spot_series.py`
  (Terminal :25503). Magnitude ≈ the existing bulk download.
- **Then run once:**
  ```
  PYTHONPATH=src:. .venv/bin/python scripts/study_D_conditioning_replication.py \
      --chains data/chain_snapshots_2024 --spots data/spot_series_2024 --mode fresh-run
  ```
- Apply the pre-registered verdict table (pre-reg §6) to the **fresh** SPREAD / t_cond /
  DSR. If the causal secondary flips it, downgrade to WEAK.

**Until that runs, D is open and B + C stay gated. No study is started.**
