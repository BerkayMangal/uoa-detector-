# Study F — pre-registration: patterns on daily OHLCV across a widened universe

**Frozen before any model is fitted (D4).** Nothing below may be changed after a
result is seen. If the thresholds are missed, the answer is "rejected", not
"retune". Registry slot 5.21 (`docs/INDEX.md` §7, "next letter F").

Written 2026-09-19, after the data was fetched and its shape measured, and before
a single estimator was constructed. Measuring the panel's dimensions is not
fitting; choosing a threshold after seeing an IC is, and that is what this
document exists to prevent.

---

## 1. The question

Does any learnable pattern on daily OHLCV rank next-session and five-session
forward returns across a 70-name universe — better than the identical search run
on labels that have been shuffled within each day?

Two horizons, h ∈ {1, 5} sessions, answered separately.

## 2. Why re-run at all, when Study E rejected

Study E's verdict stands and this study does not revisit it. What changed is the
**data**, not the ambition:

| | Study E | Study F |
|---|---|---|
| names | 10 mega-caps | 70 |
| fields | close only | open, high, low, close, volume |
| average pairwise log-return correlation | +0.361 | **+0.206** |
| first eigenvalue's share of the cross-section | 45.5% | **25.5%** |
| effective independent series (participation ratio) | 4.0 | **11.5** |
| mean daily cross-sectional dispersion | 1.72% | **3.38%** |

The standard error of a daily cross-sectional rank IC scales as roughly
1/√(N_eff), so effective breadth of 4.0 → 11.5 improves the detectable effect by
about **1.7×**: Study E could not distinguish a true |IC| below ~0.05 from zero;
this panel reaches ~0.03.

**That improvement came from breadth, not from more patterns.** Adding features
does not lower the detection floor by a single basis point; it raises the
multiplicity the null has to absorb. This is stated here so that no later reader
mistakes the feature count in §4 for the reason this study has more power than
its predecessor.

## 3. Data

- `data/study_f/bars.csv` — 70 tickers × 252 regular sessions, 2025-09-18 to
  2026-09-18, fetched by `scripts/study_f_fetch_bars.py`, one Unusual Whales
  `ohlc/1d` request per ticker, parsed through the same session filter the live
  board uses (`webapp.ohlc._regular_session_rows`). Zero missing values in any of
  the six columns. A date whose two regular rows disagree is dropped, the rule
  `daily_close._unique_bars` applies in production.
- RDFN returned an empty payload and is absent. 71 requested, 70 stored.
- **Liquidity filter, fixed now:** median daily dollar volume ≥ $20M over the
  window. This drops CHGG ($1.2M) and ROOT ($17.0M), leaving **68 names**. The
  filter is declared before fitting precisely so it cannot later be tuned to a
  number that flatters a result.
- **Label:** forward log return of the regular-session close over h sessions.
- **Warmup:** 60 sessions, the longest feature window. Usable panel is therefore
  252 − 60 − h days per name: 191 days at h=1, 187 at h=5.

### 3a. Known defects of this data, stated before the result

- **Survivorship.** The universe is a list of names that exist today and every one
  has a full 252-session history. Anything delisted, acquired or collapsed inside
  the window is absent. This inflates momentum-shaped signals. The window is short
  enough that the effect is small, but it is not zero and it is not correctable
  with the data on hand.
- **One regime, one year.** A single 12-month window. Nothing here generalises to
  a different volatility or rate regime, and no claim will be made that it does.
- **No options data.** The flow tables hold 15 usable calendar days
  (`signal`, two blocks, ~133 ticker-days). That is not enough for any pattern
  search and this study does not use them. Study E §4 already made the point.

## 4. The feature families — frozen list

Every feature is computed from bars **up to and including day t** and is used to
predict the return **after** day t. The implementation must make this structural,
not conventional: the feature function receives a prefix of the series, never the
whole series with an index.

1. **Trend / momentum.** `ret_1`, `ret_5`, `ret_10`, `ret_20`, `ret_60`,
   `ret_20_ex_5` (20-day return excluding the last 5), `ma_ratio_10_50`,
   `dist_from_252_high`, `dist_from_252_low`, `up_streak`.
2. **Range / volatility.** `atr_14_pct` (Wilder), `parkinson_10`,
   `garman_klass_10`, `vol_ratio_10_60`, `nr7`, `range_vs_atr`, `inside_bar`,
   `outside_bar`.
3. **Gap / overnight decomposition.** `gap_pct`, `overnight_ret_5`,
   `intraday_ret_5`, `gap_filled`. Only possible now that open/high/low exist.
4. **Location within the bar.** `clv` (close location value), `clv_ma_5`.
5. **Volume.** `rel_volume_20`, `volume_z_20`, `dollar_volume_log`,
   `price_volume_corr_20`, `obv_slope_20`.
6. **Mean reversion.** `rsi_14`, `zscore_close_20`, `bollinger_pos_20`.
7. **Cross-sectional / relative.** `ret_5_vs_spy`, `beta_60`, `idio_ret_5`,
   `corr_60_spy`, `xs_rank_ret_20`.
8. **Analog ("déjà vu").** For day t, standardise the feature vector using
   **training-window statistics only**, find the k = 25 nearest neighbours by
   cosine distance **among training days only**, and use the mean forward return
   of those neighbours as a prediction. Any neighbour at or after the test day is
   excluded by construction. This family is one predictor, not one feature.

Roughly 37 features across families 1–7, plus the analog predictor. The exact
count is fixed by the implementation and must not grow after a result is seen.

## 5. The models — frozen grid

Per horizon:

- **XGBoost**, 8 configurations: `max_depth` ∈ {2, 3} × `n_estimators` ∈ {100,
  300} × `learning_rate` ∈ {0.03, 0.1}, with `subsample` 0.8, `colsample_bytree`
  0.8, `min_child_weight` 5, fixed seed.
- **Ridge** on cross-sectionally ranked features, one configuration.
- **Analog predictor**, one configuration (k = 25).

**Ten configurations in total.** The reported statistic is the **best** of the ten
by mean out-of-sample rank IC — because that is what a researcher would publish,
and a null that did not also take its best would understate the search.

## 6. Protocol

- **Walk-forward.** Train 120 sessions, test 21, step 21, rolling (not expanding),
  matching Study E so the two remain comparable. Folds never overlap.
- **Standardisation and any fitted transform are computed on the training window
  only** and applied to the test window unchanged.
- **Statistic.** For each test day, the Spearman rank correlation between the
  prediction and the realised forward return across the names present that day.
  Average across test days → mean OOS rank IC for that configuration. The study's
  number is the maximum across the ten configurations.
- **Null.** Permute the labels **within each day** — preserving the cross-section,
  the market factor and every feature — then rerun the **entire ten-configuration
  search** and take its best. **200 passes.** This is the multiplicity control: the
  null distribution is a distribution of *best-of-ten*, not of a single model.
- **SHAP** is computed on the winning configuration for description only. It
  cannot change the verdict, and no feature is added, removed or re-weighted on
  the strength of a SHAP plot.

## 7. Thresholds — binding

Per horizon, in order. A failure at any threshold ends the study for that horizon.

- **T1 — beats its own null.** Mean OOS rank IC (best of ten) must exceed the
  **99th percentile** of the null's best-of-ten distribution.
- **T2 — stability.** The winning configuration's mean IC must be positive in at
  least **75%** of walk-forward folds.
- **T3 — not the market.** Re-run with the market-neutral label (`idio_ret`, the
  residual after the 60-day beta to SPY). T1 must still hold.
- **T4 — survives costs.** Only evaluated if T1–T3 pass. The implied long-short
  decile return must be positive after the repository's existing round-trip cost
  model.

If T1 fails, the verdict is **REJECTED**, the result document says so, and no
threshold, feature or configuration is changed afterwards. That is the entire
point of writing this before the run.

## 8. Pre-mortem — how this could produce a false positive anyway

Listed now so that finding one of them later is a correction, not a defence:

1. Standardisation fitted across the split boundary.
2. The analog method reaching a neighbour inside the test window.
3. A feature that silently uses `bars[t+1]` through an off-by-one.
4. Folds that overlap because the step and the test length disagree.
5. The null shuffling inside a single configuration while the real arm searches
   ten — the exact defect this design exists to avoid.
6. Ranking against a cross-section that includes names with no data that day.
7. Survivorship, per §3a.

The implementation must carry a test for 1, 2, 3 and 4 that fails when the defect
is introduced. A leak test that cannot fail is worth less than no leak test, which
this repository has learned four times (P39, P40, P44, P45).

## 9. Deliverable

`docs/study-F-result.md`, with every figure derived from committed data by a
committed script, plus a test that recomputes the published figures and fails the
gate when they drift — the arrangement Study E arrived at
(`tests/unit/test_study_e_null_evidence.py`).

The raw panel (`data/study_f/bars.csv`), the fetcher, the feature library, the
runner and the null output all ship with the result. A study whose inputs are not
in the repository is not reproducible, and this project has already decided it
would rather commit 1.2 MB than ask anyone to take a number on trust.
