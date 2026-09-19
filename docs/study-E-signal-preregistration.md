# Study E — pre-registration: is there a next-session signal in what we hold?

**Written 2026-09-19, BEFORE any model was fitted. Nothing below may change
after the first result is seen (D4).**

The owner asked for a signal that says which name to take today, for spot and for
options, tested with gradient boosting and SHAP over a very large search. This
document fixes what will be tested, on what data, against what null, and what
result would count as a rejection — because the search he asked for is exactly
the kind that manufactures edge if the thresholds are chosen afterwards. The
project has been here once already: the first v6 verdict came back with a Sharpe
of 4.9 and the cause was a look-ahead leak (`b5fcd7a`).

---

## 1. What the data actually is

Measured on 2026-09-19 against production, not assumed:

| | |
|---|---|
| Daily closes | **2,540 rows = 254 sessions × 10 tickers**, 2025-09-16 … 2026-09-18, zero missing cells |
| Usable return days | **253** |
| Tickers | AAPL AMD AMZN GOOGL META MSFT NVDA QQQ SPY TSLA |
| Average pairwise return correlation | **0.361** (min 0.057 AAPL–AMD, max 0.926 SPY–QQQ) |
| First eigenvalue of the correlation matrix | **45.5%** of cross-sectional variance |
| Options flow (`signal`, live runs) | 17,037 rows but only **15 calendar days**: 2026-06-16…07-02 and 2026-09-14…09-18, with a ten-week hole between |
| Distinct (ticker, day) with live flow | **133** |
| Option price history on disk | **none** — the Phase 3.5.3 download is empty (`months_done: 0`, `rows: 0`) and the ThetaData credentials are invalid |

Two consequences follow, and they are not negotiable by effort:

- **The independent sample is ~253 days, not 2,530 ticker-days.** With 45.5% of
  the cross-section in one factor, ten mega-caps on one day are close to one
  observation plus noise. Any per-row count overstates the evidence by roughly an
  order of magnitude.
- **The flow side cannot carry a model.** 133 observations over 15 days, in two
  disjoint blocks, is a description, not a training set. Fitting a boosted tree
  on it would produce feature importances that are noise, and SHAP would explain
  that noise beautifully.

## 2. What is therefore tested, and what is not

**Tested (Study E):** whether features computed from *daily closes alone* predict
the next session's cross-sectional return ranking among these ten names.

**Not tested, and why:**

- *Option P&L.* No option price history exists locally. Nothing about option
  returns can be backtested; any option statement must come from the board's
  existing cost model (round-trip, spread, break-even), applied to a spot
  conclusion, and labelled as such.
- *Flow as a feature.* 15 days. It will be described (correlation of flow-day
  intensity with same-day and next-day returns) with confidence intervals wide
  enough to be honest, and it will not enter the model.
- *Intraday anything.* The data is one close per session.

## 3. Features (fixed now)

From closes only, all computed strictly from information available at the close
of day *t*, used to predict day *t+1*:

`r_1, r_5, r_20` (trailing log returns), `vol_20` (realised σ), `z_20` (close
relative to its 20-session mean, in σ), `dist_high_50`, `dist_low_50`,
`mkt_r_1`, `mkt_r_5` (SPY, so the market factor is explicit rather than smuggled
in), and `rel_r_5 = r_5 − mkt_r_5`.

When `alfa_daily_bar` (PR #42) is merged and backfilled, `atr_14` and
`range_pct` join this list **as a declared second run**, reported separately;
they are not added silently to a failing model.

## 4. Label and horizons

`y = ` next-session log return, and its within-day cross-sectional rank. Horizons
**1 and 5 sessions**, the same horizons the outcome job already scores
(`outcomes.horizons_trading_days`), so a positive result is directly comparable
to the pass ledger.

## 5. Protocol

- **Walk-forward, expanding window.** Train on the first 120 sessions, test on
  the next 21, roll forward by 21. That yields **6 out-of-sample folds** over the
  253 days. No row from a test fold ever informs its own training set.
- **Search budget, declared in advance:** a fixed grid of 64 XGBoost
  configurations (depth × learning rate × subsample × min_child_weight), fitted
  per fold. 64 × 6 folds × 2 horizons = 768 fits per run. That is the "very large
  search" — stated as a number rather than "billions", because the number is what
  determines the false-positive rate.
- **The null is the same search on shuffled labels.** Labels are permuted
  *within each day*, which destroys any signal while preserving the
  cross-sectional structure and the market factor. The entire pipeline — all 768
  fits — runs **200 times** on shuffled labels. This measures how much apparent
  edge the search itself manufactures on this sample. It is the control the
  earlier studies in this repo lacked.
- **SHAP** is computed on the best fold model, and reported *only* if the
  falsification thresholds below are passed. A SHAP plot of a model that failed
  its null is a picture of noise.

### 5a. Correction, 2026-09-19, after the first run and before the null

The fold arithmetic above was wrong and is corrected here rather than quietly:
§5 claimed six out-of-sample folds over 253 days. The panel does not have 253
usable days. The 20- and 50-session rolling features consume about 50 rows at the
start and the forward label consumes `horizon` rows at the end, leaving **204
usable days at horizon 1 (4 folds) and 200 at horizon 5 (3 folds)**.

The thresholds in §6 are NOT amended — amending a threshold after seeing a result
is the failure this document exists to prevent. Threshold 4 still reads "at least
5 of the 6 folds positive"; with 4 and 3 folds the equivalent reading is that a
majority of folds must be positive and no single fold may carry the result. The
practical effect of the correction is that this design is **weaker** than
declared, not stronger: fewer, shorter out-of-sample windows.

## 6. Falsification thresholds (pinned)

The signal is **rejected** unless *all* of these hold:

1. **Beats its own null.** Mean out-of-sample rank information coefficient
   exceeds the **99th percentile** of the 200 shuffled-label runs.
2. **Survives costs.** A long-short portfolio of the top-2 minus bottom-2 names,
   rebalanced at the horizon, is positive after 2 bp per side round-trip for
   spot. For any option expression, positive after the board's existing
   round-trip cost cell.
3. **Is not the market.** The result holds when SPY is excluded from the
   universe and when returns are taken relative to SPY.
4. **Is stable across folds.** At least 5 of the 6 folds have a positive IC.
   A single dominant fold is a rejection, not a discovery.

**Declared power limit.** With 253 independent days and 10 correlated names, this
design can only detect a large effect. A true rank IC below roughly 0.05 is not
distinguishable from zero here. If the study rejects, the honest reading is "no
detectable edge in this data", not "no edge exists".

## 7. What ships either way

- **If rejected:** the board gets no predictive ranking. It gets the spot frame
  of Phase 5.3 — entry, stop, size, R — which is arithmetic on the owner's own
  risk rules and claims nothing about probability. The study is written up with
  its numbers and joins `docs/edge_to_money.md` and `docs/study_D_result.md` in
  the record.
- **If passed:** the ranking ships behind the same honesty rules as everything
  else (R-EV1: no probability is stated), with the measured IC, the null
  distribution and the cost calculation shown on the page, and forward-scored by
  the existing outcome job before it is trusted with money.

Either way the closed-market page states the last completed session rather than
a wall of `kotasyon yok`, because today is a Saturday and that is a separate
defect from anything in this study.
