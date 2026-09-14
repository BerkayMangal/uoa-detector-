# Pre-Registration — Study D: Conditioning-Signal Replication

**Committed BEFORE any computation.** This document freezes the design. Any number
produced after this commit is bound to the rules below. Deviations are logged in a
new commit, never by editing this file (CLAUDE.md D1).

## The one question

> The conditioning **long-gamma + high-IV-rank** beat blind vol-selling by **+0.77
> Sharpe** on the burned 2025-05→2026-04 panel. On a **fresh, non-overlapping**
> window — same conditioning definition, no re-tuning — does that information
> **replicate**, or was it an artifact?

D **gates** candidates B and C. If D is dead, B and C are dead-on-arrival and are
not started. If D survives, we design C together (still do not start it).

---

## 0. Data-availability finding (part of the record)

Inventoried before registering (evidence in the turn that produced this commit):

- **Burned panel** = `data/chain_snapshots/*.parquet`, 24 tickers, snapshot dates
  **2025-05-01 → 2026-04-30** (251 dates, ~8.0M rows). `scripts/edge_validation.py:_build_panel`
  globs *all* of it — the entire year × 24 tickers is burned.
- **Raw local downloads** (`data/historical/bulk`, `dry-run*`, `tier1_reduced`):
  `bulk/<TICKER>/YYYY-MM.parquet` spans exactly **2025-05 → 2026-04** (the raw source
  the panel was built from); `dry-run*` are **2026-04** only. **Max date anywhere
  locally = 2026-04-30.**
- **There is ZERO local data outside the burned window.** No pre-May-2025 data; no
  post-Apr-2026 data.

**Consequence:** a clean fresh-window test of D **cannot be run on local data**.
Execution is **BLOCKED on a fresh download** (spec in §8). Per the standing
instruction "if there isn't enough fresh data, STOP — don't proceed with a bad
window," D is **not** run on the burned panel, nor on a too-short forward stub, nor
on a same-period different-ticker re-slice. The design below is frozen now so that
the moment fresh data exists the test runs with zero researcher degrees of freedom.

---

## 1. Fresh window (mechanical rule, fixed before seeing any result)

**Selection rule (no cherry-pick):** the **longest contiguous historical window of
comparable length to the original (≈12 months) that has ZERO calendar overlap with
the burned 2025-05-01→2026-04-30 panel**, taken as the period **immediately preceding**
the panel.

**Registered primary window:** **2024-05-01 → 2025-04-30** (≈250 trading days).
- Zero overlap with the burned panel. ✓
- Untouched: never used in any study. ✓
- Length ≈ original → comparable statistical power (original conditioned arm was
  n≈63 non-overlap; a full prior year should be in the same ballpark). ✓
- Downloadable now (does not require waiting for forward data to accrue). ✓

**Universe (frozen ex-ante, NOT re-selected on D's results):** the **same fixed
24-ticker set** as the burned panel — `ABNB AI AMD BAC COIN CRWD DKNG GOOGL HOOD JNJ
JPM LCID MARA META PLTR PLUG RBLX RIOT RIVN SNAP SOFI TSLA UNH XOM`. Holding the
universe constant tests the *signal*, not the universe. This is the no-cherry-pick
guarantee: the universe is inherited, never chosen to make D pass.

**Per-observation liquidity / validity:** inherited, not re-defined — an observation
is valid iff `compute_gamma` and `atm_iv` both return non-None (they already drop
illiquid / empty chains: `compute_gamma` requires OI>0, IV>0, t>0; `atm_iv` requires
≥1 ATM strike DTE 10-45). `_build_panel`'s drop rules are inherited verbatim.

**Stricter alternative (registered, currently infeasible):** the *forward* window
**2026-05-01 → present** is the purest live-OOS test, but as of 2026-06-24 it is only
~7 weeks (~35 trading days) → at `_HOLD=20` it yields ≈1 non-overlap obs/ticker
(~24 total, conditioned arm a handful) → **underpowered, disqualified now.** Revisit
once ≥6 months of forward data exist (~Nov 2026+).

---

## 2. Conditioning definition — INHERITED EXACTLY (no re-tuning)

Cited verbatim from code; not one threshold is changed.

- **Net GEX:** `webapp/gamma.py:compute_gamma(chain, as_of=...)["net_gex"]`
  = `Σ sign · OI · BS_gamma(spot)`, `sign=+1` call / `−1` put (line 147, 152).
  Convention (line 125): `net_gex > 0` ⇒ dealers **long** gamma (suppress);
  `< 0` ⇒ **short** gamma (amplify).
- **ATM IV:** `webapp/gamma.py:atm_iv(chain, as_of=...)` = median IV of options
  DTE∈[10,45] within 5% of spot (lines 108-110).
- **Conditioning** (`scripts/edge_validation.py:_build_panel`, lines 106-108):
  - `regime = "short" if net_gex < 0 else "long"`
  - `iv_pct = df.groupby("ticker")["iv"].rank(pct=True)`  (per-ticker percentile)
  - `sell_signal = (regime == "long") & (iv_pct >= 0.75)`   (`_IV_HI = 0.75`)
- **Hold / annualisation:** `_HOLD = 20` trading days, `_T = 20/252`,
  Sharpe annualised `× √(252/20)` (inherited `_sharpe`); honest n via non-overlap
  `g.iloc[::20]` per ticker (inherited `_nonoverlap`).

**Known look-ahead in the inherited definition (flagged, not silently fixed).**
`iv_pct` ranks each day's IV against the *full window* (including future days) — a
mild in-window look-ahead. The dominant instruction is "inherit the conditioning
exactly," so the **primary** test uses it as-is (re-defining it would be the tuning
this study forbids). A **secondary, causal** variant — IV percentile from a *trailing
expanding window only* — is computed alongside as an honesty check. The verdict (§6)
is on the **primary**; the secondary tells us whether any "survived" is look-ahead-
contaminated.

---

## 3. Baseline

**Unconditioned** = all valid ticker-day observations in the same fresh window and
the same 24-ticker universe (no conditioning gate). This is the "blind vol-selling"
comparator, exactly as in the original baseline section of `edge_validation.py`.

---

## 4. Test statistic — pure information, NO position, NO tail

For each valid non-overlap observation *i* with entry spot `s_in`, +20-trading-day
spot `s_out`, and entry `iv`:

```
implied_move_i  = s_in_i · iv_i · √(20/252)
realized_move_i = |s_out_i − s_in_i|
vrp_i           = (implied_move_i − realized_move_i) / s_in_i        # vol-points/$spot
```

`vrp_i > 0` ⇒ vol was overpriced vs what realised (good for a hypothetical seller).
**No option is priced, no spread/commission charged, no gap simulated** — this is the
information content of the conditioning, not a tradeable P&L (that is B/C's job, and
only if D survives).

- **COND arm** = `{i : sell_signal_i}` ; **UNCOND arm** = all valid obs.
- `Sharpe(arm) = mean(vrp)/std(vrp, ddof=1) · √(252/20)`.
- **PRIMARY STATISTIC:** `SPREAD = Sharpe(COND) − Sharpe(UNCOND)`
  — the vol-points analog of the documented +0.77 fly-return spread. *(Honesty note:
  the original +0.77 was an iron-butterfly **return** Sharpe spread, a traded metric;
  this VRP spread is the pure-information analog the question asks for. Same sign and
  significance ⇒ the information replicated; the literal value need not equal 0.77.)*
- **Secondary reported:** IC = Pearson corr(`sell_signal`∈{0,1}, `vrp`) over all valid
  obs (point-biserial); `t_cond` = `mean(vrp_COND)/(std/√n_COND)` (H0: mean=0);
  Welch t of (COND − UNCOND); **Deflated Sharpe Ratio of the COND arm, `n_trials=1`**
  (pre-registered single hypothesis — the forking-paths shield; DSR here only deflates
  for skew/kurtosis/length, not for a search).
- Report `n_COND` and `n_UNCOND` (non-overlap counts).

---

## 5. Discipline (pre-committed)

- Single window (§1), single run, single hypothesis (`n_trials=1`).
- Conditioning inherited verbatim (§2). **No threshold re-tuned on fresh data.**
- Look-ahead: conditioning uses only the entry-date snapshot for `net_gex`/`iv`;
  `s_out` (the outcome) never enters the gate. Inherited `iv_pct` look-ahead flagged
  and bracketed by the causal secondary (§2).
- Burned panel **not** reused for the verdict.
- After seeing the result: no window change, no ticker selection, no threshold edit.

---

## 6. Pre-committed verdict thresholds

On the **primary** statistic, fresh window:

| Outcome | Condition | Action |
|---|---|---|
| **DEAD** | `SPREAD ≤ 0` | Conditioning was an artifact. **D dead. B and C dead-on-arrival** — not started. Done. |
| **WEAK** | `SPREAD > 0` **but** (`|t_cond| < 2` **or** `DSR ≤ 0.95`) | Not robustly replicated. **No green light** to B/C. |
| **SURVIVED** | `SPREAD > 0` **and** `|t_cond| ≥ 2` **and** `DSR > 0.95` | Information replicated. **STOP — design C together.** Do not start C. |

If the **secondary causal** variant flips the verdict (primary SURVIVED but causal
DEAD/WEAK), the headline survival is treated as look-ahead-contaminated and downgraded
to **WEAK** — no green light.

---

## 7. Outputs

- `scripts/study_D_conditioning_replication.py` — parameterised by data dir, so the
  *same* code runs on the burned panel (instrument check only) and, when it exists,
  the fresh window (the real single run). `scripts/` is outside the `mypy --strict src/`
  gate (consistent with all existing tracked scripts).
- `docs/study_D_result.md` — the verdict (or BLOCKED status until fresh data exists).

---

## 8. Execution status: BLOCKED — required fresh download

D cannot run until the registered fresh window exists locally. Required (Berkay's
job — historical ThetaData download is the operator's responsibility, CLAUDE.md):

- **Window:** 2024-05-01 → 2025-04-30.
- **Tickers:** the 24 listed in §1.
- **What:** daily option-chain snapshots (strike, expiry, option_type, open_interest,
  implied_volatility, spot) → `data/chain_snapshots_2024/` (NOT the burned dir), plus
  spot series → `data/spot_series_2024/`. Same schema as the current dirs.
- **How:** the same bulk pipeline that produced the panel
  (`scripts/download_chain_snapshots.py` → `scripts/compute_spot_series.py`,
  ThetaData Terminal on :25503). Magnitude ≈ the existing bulk download (~8M rows);
  this is the multi-hour operator job, not an in-session call.
- **Then:** point `scripts/study_D_conditioning_replication.py` at the new dirs and
  run **once**.

**Nothing is run on a substitute window. The design ends here, frozen.**
