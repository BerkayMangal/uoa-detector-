# Study E — result: no detectable signal, and the search is why

**Protocol and thresholds fixed in `docs/study-E-signal-preregistration.md`
before any model was fitted (D4). Nothing below changes them.**

Run 2026-09-19 over 200 shuffled-label passes. Every figure below is derived from
`data/study_e/null_means.jsonl`, which ships with this document, not typed: the §1
cells were rewritten three times as the run grew, at n=95, 98 and 191, once in the
direction that flattered the signal. `tests/unit/test_study_e_null_evidence.py`
now recomputes every one of them from that file, so a figure that drifts again
turns the gate red instead of reaching a reader.

---

## 1. Verdict

**Rejected.** Both horizons fail threshold 1, and fail it by a wide margin.

| horizon | real mean OOS rank IC | null mean | null sd | null p99 | percentile of the real result | shuffled draws beating it |
|---|---|---|---|---|---|---|
| 1 session | **+0.0253** | +0.0027 | 0.0335 | +0.0729 | **75.0th** | 50 / 200 |
| 5 sessions | **+0.0239** | +0.0025 | 0.0431 | +0.0950 | **67.5th** | 65 / 200 |

Threshold 1 asked the real result to exceed the **99th percentile** of its own
null. It reaches the **75.0th** at best. Roughly one shuffled draw in
3 beats it, and the real information coefficient would have to be about
2.9 times larger to clear the bar.

Threshold 4 (stability) also fails on its own: 2 of 4 positive folds at horizon 1
(−0.017, −0.077, +0.097, +0.099) and 2 of 3 at horizon 5 (+0.020, −0.159,
+0.211). The per-fold spread is wider than the effect.

Thresholds 2 (costs) and 3 (not the market) were never reached. A signal that
does not clear its null is not worth costing out.

## 2. What the null proves, which is the point of the study

The shuffled-label distribution is centred on zero — its mean is a few
thousandths of an IC point, against a spread ten times that — which is the harness
certifying itself: permuting labels **within each day** destroys
the signal while preserving the cross-sectional structure and the market factor,
and the search then finds nothing on average, as it must.

But its **spread is large**: sd 0.033 and 0.043, and its 99th
percentiles sit at +0.0729 and +0.0950. So a 64-configuration grid
over this sample manufactures an apparent IC of that size from pure noise about as
easily as the real labels produce +0.0253.

That is the finding. Not "the model is weak" but **the search is stronger than
the data**. Without this control the real +0.0253 would have looked like a
result, a SHAP plot would have explained it convincingly, and the explanation
would have been of noise. This repo has shipped exactly that mistake once
before: the first v6 verdict came back with a Sharpe of 4.9 and the cause was a
look-ahead leak (`b5fcd7a`).

## 3. Why the data cannot support more, in numbers

- **253 independent return days, not 2,530 ticker-days.** Average pairwise
  correlation of **log** returns 0.361; the first eigenvalue of that correlation
  matrix holds **45.5%** of the cross-section. Ten mega-caps on one day are close
  to one observation plus noise.
- **The walk-forward yields 4 folds at horizon 1 and 3 at horizon 5**, not the 6
  declared (§5a records the correction): the 20- and 50-session features consume
  ~50 rows at the start and the label consumes `horizon` at the end. What the
  model actually saw is therefore **204 days at horizon 1 and 200 at horizon 5**,
  not 253.
- **Declared power limit, from the pre-registration:** a true rank IC below
  roughly 0.05 is not distinguishable from zero here. The measured +0.025 is
  inside that blind spot. **This is "no detectable edge in this data", not "no
  edge exists".**

## 4. The options flow, described rather than modelled

17,037 live rows exported from the board's own `signal` table, but **15 calendar
days** in two blocks ten weeks apart, giving **133 (ticker, day) observations**.
The score is that table's `combined_score_post`; `combined_score_pre` differs on
817 of the 17,037 rows and produces a different table, so the column is named here
rather than left for a reader to guess. Rank correlations against returns, each
column using every observation for which that horizon exists (**n = 133, 123 and
83**), and intervals computed on the number of **independent days** rather than
rows:

| feature | vs same-day | vs next session | vs 5 sessions |
|---|---|---|---|
| print count | +0.069 | −0.037 | −0.028 |
| mean score | +0.075 | −0.123 | **−0.306** |
| max score | +0.084 | −0.125 | **−0.314** |

The two bold figures come with p = 0.005 and p = 0.004 — and they are **not
findings**. Those p-values treat 83 ticker-days as 83 independent observations;
there are **10 distinct days** behind them. Corrected, the 95% interval is
**[−0.78, +0.40]**: it spans zero and most of the possible range. Reported
without that correction, this would have shipped as "flow predicts five-day
reversal", built on ten days.

**Option P&L was not backtested at all** and could not be: there is no option
price history on disk (the Phase 3.5.3 download is empty — `months_done: 0`,
`rows: 0`) and the ThetaData credentials are invalid. Any option statement on the
board therefore comes from the existing round-trip cost model applied to a spot
frame, never from a fitted return.

## 5. Completion

The pre-registration asked for 200 shuffled passes and **200 completed**. The run is
resumable and persists one JSON line per rep, which it had to be: the OS killed it
three times under memory pressure and every result survived each kill.

**The verdict does not depend on the count.** The real result's percentile against its
own null, recomputed at increasing n:

| n | h1 null p99 | h1 percentile | h5 null p99 | h5 percentile |
|---|---|---|---|---|
| 25 | +0.0550 | 68.0% | +0.0885 | 72.0% |
| 50 | +0.0635 | 72.0% | +0.0890 | 74.0% |
| 75 | +0.0625 | 77.3% | +0.0890 | 77.3% |
| 100 | +0.0734 | 75.0% | +0.0890 | 74.0% |
| 200 | +0.0729 | 75.0% | +0.0950 | 67.5% |

Across every n from 1 to 200, the real result peaks at the **87.5th** percentile —
horizon 5 at n=8, where eight draws are not yet a distribution. From n=25 on it
never rises above the **78.5th**, and horizon 1 never exceeds the **77.8th**. The
threshold was the 99th.

(An earlier version of this line said "never above the 78th at any n". That was
false at seven values of n, and the test quoting it took its maximum over only the
five sizes tabulated above, so it could not contradict the quantifier it claimed to
guard. Both were corrected after the 2026-09-19 audit; the verdict is unchanged,
because 87.5 is no closer to 99 than 78 was.)
Stopping short of 200 would not have changed the answer, and reaching it did not.

## 6. What ships, per §7 of the pre-registration

**No predictive ranking reaches the board.** What ships instead is the Phase 5.3
spot frame: entry, ATR-based stop, share count floored to the owner's R, dollars
actually risked, the expected move as a target, and R to reach it. Every one of
those is arithmetic on the owner's own risk rules and states no probability
(R-EV1, R-SP4). The board keeps ranking rows by **evidence**, as it already does,
and says nothing about the odds of a trade working.

Study E joins `docs/edge_to_money.md`, `docs/study_D_result.md` and
`docs/phase-3.6-closeout.md` in the record. The canonical edge statement in
`docs/INDEX.md` §0 is unchanged by this study and is now supported by one more
rejection with an explicit null behind it.

## 7. Reproduction

Everything the verdict rests on is in the repository:

| what | where |
|---|---|
| the protocol, frozen before any fit | `docs/study-E-signal-preregistration.md` |
| the search itself | `scripts/study_e_signal.py` |
| the resumable null runner | `scripts/study_e_null.py` |
| the closes the study ran on, 254 sessions x 10 tickers | `data/study_e/closes.csv` |
| 200 shuffled passes, one JSON line each | `data/study_e/null_means.jsonl` |
| the flow aggregate behind §4, 133 rows | `data/study_e/flow_daily.csv` |
| every figure above, recomputed on each test run | `tests/unit/test_study_e_null_evidence.py` |

To rerun the null from scratch, which takes about an hour:

```
uv run python scripts/study_e_null.py data/study_e/closes.csv fresh.jsonl 200
```

The null was executed against a working copy of the protocol module rather than
the committed file. The two are identical across every symbol the runner reaches
— `load_closes`, `build_panel`, `run_once`, `rank_ic`, `folds`, `FoldResult` and
the seven constants including `GRID` and `FEATURES` — verified by hashing each
symbol's normalised source: md5 `e23d7e12e50c9eadb545da3f29f6100f` on both. The
working copy's only differences are an unused import and an `int()` cast inside
`main()`, which the runner never calls.

> **Amended 2026-09-20 (audit).** `scripts/study_e_signal.py` now **purges the fold
> boundary**: the training window ends `horizon - 1` sessions before the test window
> opens, and the same purge applies to the inner split that selects the
> configuration. It did not before, and the 2026-09-19 audit was right to call that a
> defect — at horizon 5, fold 0's last training day carried a label built entirely
> from sessions inside the test fold, which contradicts this study's own
> pre-registration ("no row from a test fold ever informs its own training set").
>
> Two consequences, stated rather than buried. **The md5 above no longer matches the
> committed file**, because `folds` and `run_once` changed; it records what produced
> the published figures, which is what a reproduction note is for. And **the figures
> in §1 and §5 were produced without the purge**, so the runner as committed will not
> reproduce them exactly. A purged re-run is owed and is not being presented as done.
>
> The verdict is unchanged, and the reason is directional: the leak let training
> labels see test-window prices, which can only **inflate** the real arm's IC, while
> the null permutes within each day and so destroys the cross-sectional content of
> exactly those leaked returns. A rejection that held with the real arm inflated
> holds without it. Study F, whose pre-registration was amended before its runner
> existed precisely so it would never be in this position, reached the same verdict
> on a panel seven times wider (`docs/study-F-result.md`).

The 17,037 raw prints behind §4 are **not** committed: 2.5 MB of vendor row-level
data, re-exportable from the `signal` table. What ships is the aggregate the study
actually modelled, so §4's nine correlations are recomputed by the test above from
that file and the closes.
