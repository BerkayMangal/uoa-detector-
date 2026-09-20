# Study G — pre-registration: residual-return ranking, scored forward

**Frozen 2026-09-20, before a single session of its data exists.** Registry slot
5.22 (`docs/INDEX.md` §7). Nothing below may change after a result is seen; if the
thresholds are missed the answer is "rejected", and this document is the reason
that answer will mean something.

This is the study `docs/study-F-result.md` §4 says is owed, written the only way it
can honestly be written.

---

## 1. Why this exists, stated plainly

Study F searched ten configurations over 37 daily-OHLCV features on a 70-name panel
and **rejected** both horizons against a best-of-ten shuffled-label null. One cell
of it did not fail: the market-neutral label at a five-session horizon produced a
mean OOS rank IC of **+0.0505**, above its null's 99th percentile of +0.0426, with
**zero of 200** shuffled draws reaching it.

Study F refused to call that a finding, and was right to. Its §7 fixed the order —
T1 on the forward-return label, T3 only after T1 holds — and horizon 5's forward arm
sat at the 47.5th percentile. Promoting the confirmation to primary after watching
the primary fail is the move D4 forbids.

So the observation is real and the study that produced it cannot evaluate it. That
is what a pre-registration is for.

## 2. Why it must be scored forward, not backward

Measured 2026-09-20: `GET /api/stock/{t}/ohlc/1d` returns exactly **252 regular
sessions** — 2025-09-18 to 2026-09-18 for SPY, PLTR and AAPL alike, with zero
sessions before Study F's window opened. There is no unsearched window on disk. The
one vendor with a longer daily history is ThetaData, whose credentials are invalid
(`docs/study-E-result.md` §4) and whose bulk download is out of scope.

Running this hypothesis on Study F's own window would be the thing §4 refuses.
Running it on sessions that did not exist when the hypothesis was frozen is
out-of-sample **by construction** rather than by assertion, which is strictly
stronger than any split of an existing panel.

The cost is time, and it is stated rather than hidden: the first evaluation cannot
happen before roughly **2027-04**, and §6 fixes the date rule instead of leaving it
to whoever is impatient.

## 3. The question

Does a ranking learned from daily OHLCV predict the **residual** five-session
forward return — the forward return minus beta times the market's forward return,
with beta from the trailing 60 sessions — across the same 68-name liquid universe,
better than the identical search run on labels shuffled within each day?

One horizon. One label. No forward-return arm, because that was already rejected and
re-testing it here would spend multiplicity on a question with an answer.

## 4. What is frozen, and deliberately not extended

Everything reusable is reused **unchanged**, because the point of this study is a
clean window, not a wider search:

- **Features:** the 37 of `src/uoa_detector/research/features.py`, exactly as
  committed. No feature may be added, removed or redefined. Adding features would
  lower no detection floor and would raise the multiplicity the null must absorb —
  Study F's §2 measured that and this study inherits the conclusion.
- **Models:** the same ten configurations — eight XGBoost
  (`max_depth` ∈ {2,3} × `n_estimators` ∈ {100,300} × `learning_rate` ∈ {0.03,0.1}),
  ridge on cross-sectionally ranked features, and the k=25 analog predictor.
- **Protocol:** rolling walk-forward, train 120 / test 21 / step 21, with the
  training window ending `horizon − 1` = 4 sessions before each test window opens
  (`docs/study-F-preregistration-addendum.md`).
- **Statistic:** per test day, the Spearman rank correlation between prediction and
  realised residual return; averaged across test days per fold, then across folds.
  The study's number is the maximum across the ten configurations.
- **Null:** labels permuted **within each day**, the entire ten-configuration search
  rerun, its best taken. **200 passes.**
- **Universe and liquidity:** the same 68 names Study F modelled, i.e. the committed
  universe filtered at median daily dollar volume ≥ $20M **measured over the new
  window**. A name that falls below the line in the new window is dropped; the rule
  is fixed now, its outcome is not.
- **Runner:** `scripts/study_f_runner.py --label idio --horizon 5`. If it needs a
  change to run on the new panel, the change must be mechanical (paths, date
  filtering) and must leave `tests/unit/test_study_f_runner.py` green, including its
  five mutation-proven leak guards.

## 5. The data

- **Window:** regular sessions with `date >= 2026-09-19` — strictly after Study F's
  panel ends. Sessions on or before 2026-09-18 are **excluded**, whatever the fetch
  returns.
- **Fetcher:** `scripts/study_f_fetch_bars.py`, run periodically into
  `data/study_g/bars.csv`. UW's 252-session window rolls forward, so monthly runs
  accumulate the new sessions; the fetcher is resumable and its coverage sidecar
  records what each ticker actually returned.
- **Warmup:** 60 sessions, as the longest feature window requires. These come from
  **inside** the new window, not from Study F's panel: a feature computed across the
  boundary would import information the frozen hypothesis already saw.
- **Minimum to evaluate:** 60 warmup + 120 train + 21 test + 4 purge + 5 horizon =
  **210 sessions**, which is about 10 months of trading. One fold. §6 says what
  happens then.

## 6. Thresholds and the stopping rule — binding

- **T1 — beats its own null.** Mean OOS rank IC (best of ten) must exceed the
  **99th percentile** of the null's best-of-ten distribution over 200 passes.
- **T2 — stability.** The winning configuration's per-fold IC must be positive in at
  least **75%** of folds. With one fold that is 1 of 1; with three it is 3 of 3.
  This is stricter than it looks and is being fixed anyway.
- **T3 — survives costs.** Evaluated only if T1 and T2 pass: the implied long-short
  decile return must be positive after the repository's existing round-trip cost
  model (`backtest.slippage_pct` 0.02 plus $0.65 per contract per leg).
- **Stopping rule.** The study is evaluated **once**, on the first date the window
  reaches 210 sessions, and again **only** at 331 sessions (a second fold) and 452
  (a third). Three evaluations, dates determined by the calendar rather than by the
  result. No evaluation may be triggered by looking at the data, and a failed
  evaluation may not be repeated with more sessions in the hope of a different
  answer — the later checkpoints exist because they were declared here, not because
  the first one disappointed.
- If T1 fails at all three checkpoints, the verdict is **REJECTED** and
  `docs/study-G-result.md` says so.

## 7. Pre-mortem — how this could produce a false positive anyway

1. A feature reaching across the 2026-09-19 boundary into Study F's window, through
   warmup or through the market series.
2. Beta estimated on the 60 sessions that include the label's own window.
3. The liquidity filter applied over the wrong window, so survivors are chosen with
   hindsight.
4. The stopping rule bent — an evaluation run because the number looked good.
5. The market-neutral label hiding a market-timing signal: if the residual is
   computed with a beta that is stale, part of the market return leaks into the
   label and a model that predicts the index scores as if it predicted the residual.
6. Multiplicity smuggled in as "mechanical" runner changes.

Items 1, 2, 3 and 5 must each carry a test that fails when the defect is
introduced, as Study F's §8 required and its runner demonstrated. Item 4 cannot be
tested and is the reason the dates are written above.

## 8. What this study cannot do

It cannot rescue the forward-return hypothesis, which Study F rejected on a panel
seven times wider than Study E's. It cannot produce a tradeable strategy on its own:
T3 is a gate, not a backtest, and clearing it would license a *further* study with
its own FDR budget rather than a position. And it cannot say anything about a
different volatility or rate regime than the one its sessions happen to fall in.

**Nothing from this study reaches the board before it has a result.** No ranking, no
score, no probability. The board keeps ordering rows by evidence and stating no odds
(R-EV1, R-SP4).
