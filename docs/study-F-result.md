# Study F — result: a wider panel, the same answer, and a null that explains why

**Protocol and thresholds fixed in `docs/study-F-preregistration.md` before any
model was fitted (D4), amended once by
`docs/study-F-preregistration-addendum.md` before the runner existed. Nothing
below changes them.**

Run 2026-09-19/20. Every figure is derived from `data/study_f/null_forward.jsonl`
and `data/study_f/null_idio.jsonl`, which ship with this document, by
`tests/unit/test_study_f_null_evidence.py` on every gate run. 402 passes per
label: one real arm and 200 shuffled-label passes at each of two horizons, each
pass a complete ten-configuration search.

---

## 1. Verdict

**Rejected on both horizons.** Threshold T1 asked the best-of-ten mean
out-of-sample rank IC to exceed the **99th percentile** of its own
shuffled-label null. Neither horizon does.

| horizon | real best-of-ten IC | winning configuration | null mean | null sd | null p99 | percentile of the real result | shuffled draws beating it |
|---|---|---|---|---|---|---|---|
| 1 session | **+0.0400** | `xgb_d3_n100_lr0.03` | +0.0165 | 0.0126 | +0.0497 | **97.5th** | 5 / 200 |
| 5 sessions | **+0.0139** | `xgb_d3_n300_lr0.1` | +0.0157 | 0.0119 | +0.0417 | **47.5th** | 105 / 200 |

Horizon 1 comes closest and still misses: +0.0400 against a null 99th percentile
of +0.0497. The IC would have to be about **1.24 times larger** to clear the bar.
Horizon 5 is not close to anything — its real result sits below the null's own
mean, and **more than half** of the shuffled draws beat it.

T2 (stability) is reached only by horizon 1, which passes it: the winning
configuration's IC is positive in **3 of 3** folds. Horizon 5's winner is positive
in 2 of 3, which is 67% against the 75% the pre-registration fixed, so horizon 5
fails T2 as well as T1.

T3 (not the market) and T4 (survives costs) were **never reached**, because T1 is
first in the order and both horizons fail it. Section 4 records what the
market-neutral arm did anyway, and why the protocol does not let it be a finding.

## 2. What the null proves, which is the point of the study

The shuffled-label distribution is centred near zero — mean +0.0165 and +0.0157
of an IC point — which is the harness certifying itself: permuting labels **within
each day** destroys the signal while preserving the cross-section, the market
factor and every feature, and the search then finds almost nothing on average.

But the null's **99th percentiles are +0.0497 and +0.0417**, and its maxima are
+0.0593 and +0.0514. A ten-configuration search over this panel manufactures an
apparent IC of that size from pure noise about as readily as the real labels
produce +0.0400.

That is the finding, and it is the same one Study E reached on a narrower panel:
**the search is stronger than the data.** Without this control, horizon 1's
+0.0400 with 3-of-3 positive folds would have read as a result, SHAP would have
explained it convincingly, and the explanation would have been of noise.

The null also says something about which models find noise most easily. Across
200 passes the best-of-ten was won by `ridge` 41 times and by the analog
predictor 33 times at horizon 1 — the two configurations that score **worst** on
the real labels (`ridge` −0.0307, `analog` +0.0110). A configuration that wins on
shuffled labels is not a good model; it is a flexible one.

## 3. Why a 2.9× wider panel did not change the answer

The pre-registration's §2 was explicit that the improvement came from breadth,
not from patterns, and quantified it before any fit: effective independent series
4.0 → 11.5, so the detectable effect improves about 1.7×, taking the floor from
|IC| ≈ 0.05 to ≈ 0.03.

That prediction held, and it was not enough:

- **The measured ICs are inside the new floor.** +0.0400 at horizon 1 is barely
  above ≈0.03, and the null's spread (sd 0.0126) is a third of the measurement.
- **Three folds, not six.** 252 sessions minus 60 warmup minus the horizon leaves
  191 usable days at horizon 1 and 187 at horizon 5. With train 120 / test 21 /
  step 21 — all frozen — that yields **3 folds**, and the last 8 sessions are
  never tested. T2's 75% therefore means 3 of 3 in practice: two positive folds
  out of three is 67%. This is a consequence of the frozen protocol, recorded
  rather than adjusted.
- **37 features raised the multiplicity the null had to absorb**, exactly as §2
  said they would. The null's p99 on this panel (+0.0497 at horizon 1) is lower
  than Study E's (+0.0729) because ten configurations search less than sixty-four,
  not because the data is cleaner.
- **One regime, one year, survivors only.** §3a's defects are unchanged: a single
  12-month window, and a universe of names that all still exist with full
  history, which inflates momentum-shaped signals.

**This is "no detectable edge in this data", not "no edge exists."** The declared
power limit was stated before the run and the result sits inside it.

## 4. The market-neutral arm, and why §7's order does not let it be a finding

T3 asked for the same search on the market-neutral label — the forward return
minus beta times the market's forward return, with beta from the same 60-session
window the feature library uses. It was pre-registered, it was run, and it
produced the only T1-style pass in the study:

| horizon | real best-of-ten IC | null mean | null p99 | null max | percentile | draws beating it |
|---|---|---|---|---|---|---|
| 1 session (idio) | +0.0412 | +0.0167 | +0.0423 | +0.0451 | 98.0th | 4 / 200 |
| 5 sessions (idio) | **+0.0505** | +0.0164 | +0.0426 | +0.0474 | **100.0th** | **0 / 200** |

At horizon 5 the market-neutral label beats its own null's 99th percentile, and
**not one of 200 shuffled draws reaches it**. Its stability is 2 of 3 folds.

**The protocol does not let this be a finding, and it is not being reported as
one.** §7 fixes the order: T1 is defined on the forward-return label, T3 is a
confirmation that runs only if T1 has already held. Horizon 5's forward arm sits
at the 47.5th percentile of its null. Promoting T3 to the primary test after
seeing that it passed while the primary failed is precisely the move D4 exists to
forbid, and it is the move that would turn this study into the p-hacking this
repository has avoided from day one.

What the number is worth is a **pre-registration for someone else**: a study whose
primary hypothesis is "residual-return ranking at a five-session horizon", with
its own frozen protocol, its own null, and its own FDR budget, on a window this
one has not already searched. Until that exists, this cell is an observation
recorded honestly, with its provenance in the shipped file, and nothing more. It
is not evidence, it does not reach the board, and no position may be sized on it.

**And that window does not currently exist.** Measured 2026-09-20, after this
result was written: `GET /api/stock/{t}/ohlc/1d` returns exactly 252 regular
sessions — the same 2025-09-18 to 2026-09-18 span for SPY, PLTR and AAPL, with
**zero** sessions before this study's window opened. The ~752 rows the
pre-registration noted are pre-market, regular and post-market rows of those same
252 days, not a longer history. The vendor with a longer daily history is
ThetaData, whose credentials are invalid (`docs/study-E-result.md` §4) and whose
bulk download is out of scope.

So the follow-up this section calls for cannot be run backwards on the data
available. It can only be run **forwards**: freeze the hypothesis and the
thresholds now, let the sessions accumulate, and score it when the window is long
enough to be out-of-sample by construction rather than by assertion. That is done —
`docs/study-G-preregistration.md`, frozen 2026-09-20 before a single session of its
data existed, reusing this study's features, models, protocol and null unchanged so
the search does not widen, with three evaluation dates fixed in advance and the
first no earlier than roughly 2027-04. Running it on
this window instead — the only one on disk — is the move the paragraph above
refuses, and measuring the constraint does not make the refusal weaker. It makes
it the only honest option, which is why it is recorded here rather than left for a
later reader to discover while looking for a way to use the number.

## 5. Completion, and the verdict's independence from the pass count

Both labels completed **200 of 200** shuffled passes at both horizons; the reps
are complete and in order with no gaps, which a resumable runner has to be
checked for rather than assumed.

The verdict does not depend on the count — but one cell shows why the count was
fixed in advance:

| n | h1 null p99 | h1 percentile | h5 null p99 | h5 percentile |
|---|---|---|---|---|
| 25 | +0.0382 | 100.0% | +0.0404 | 48.0% |
| 50 | +0.0521 | 98.0% | +0.0404 | 52.0% |
| 75 | +0.0405 | 97.3% | +0.0417 | 52.0% |
| 100 | +0.0497 | 97.0% | +0.0417 | 50.0% |
| 200 | +0.0497 | 97.5% | +0.0417 | 47.5% |

**At n=25 horizon 1 would have passed T1** — its real +0.0400 exceeds that
prefix's p99 of +0.0382. By n=50 the null's tail has filled in and the answer
inverts, and it stays inverted through n=200. A study that had stopped when the
number looked good would have published an edge. This is what pre-registering the
pass count buys, and it is the second time this repository has measured it: Study
E's §5 made the same check and found its verdict stable at every n.

Horizon 5 never comes close at any n: its percentile peaks at 70.0 over all n and
at 56.8 once n ≥ 25.

## 6. What ships

**Nothing from this study reaches the board.** No ranking, no score, no
probability. What the owner sees is unchanged: the Phase 5.3 spot frame — entry,
ATR stop, share count floored to his own R, dollars actually risked, the expected
move as a target and R to reach it — every line of it arithmetic on his own risk
rules, stating no odds (R-EV1, R-SP4). Rows are ordered by evidence, as before.

Study F joins `docs/study-E-result.md`, `docs/edge_to_money.md`,
`docs/study_D_result.md` and `docs/phase-3.6-closeout.md` in the record. The
canonical edge statement in `docs/INDEX.md` §0 is unchanged by this study and is
now supported by one more rejection with an explicit null behind it — this time on
a panel seven times wider, with full OHLCV rather than closes alone, which
removes "the data was too narrow" as an explanation for the previous rejection.

## 7. Reproduction

| what | where |
|---|---|
| the protocol, frozen before any fit | `docs/study-F-preregistration.md` |
| the purge amendment, committed before the runner existed | `docs/study-F-preregistration-addendum.md` |
| the universe, 71 requested | `data/study_f/universe.txt` |
| the panel, 70 tickers x 252 sessions | `data/study_f/bars.csv` |
| per-ticker session coverage | `data/study_f/bars.coverage.csv` |
| the fetcher | `scripts/study_f_fetch_bars.py` |
| the 37-feature library, prefix-only by signature | `src/uoa_detector/research/features.py` |
| the search, its purge and its null | `scripts/study_f_runner.py` |
| the four thresholds applied to the output | `scripts/study_f_verdict.py` |
| 402 forward-label passes, one JSON line each | `data/study_f/null_forward.jsonl` |
| 402 market-neutral passes | `data/study_f/null_idio.jsonl` |
| every figure above, recomputed on each test run | `tests/unit/test_study_f_null_evidence.py` |
| the leak tests §8 requires, each proven by mutation | `tests/unit/test_study_f_runner.py` |

To reproduce from the committed panel (about 45 minutes per label):

```
uv run --group research python scripts/study_f_runner.py \
    data/study_f/bars.csv out/forward.jsonl --null 200
uv run --group research python scripts/study_f_runner.py \
    data/study_f/bars.csv out/idio.jsonl --label idio --null 200
uv run --group research python scripts/study_f_verdict.py \
    out/forward.jsonl --idio out/idio.jsonl
```

The runner is resumable: it skips (horizon, rep) pairs already present in its
output, so a killed run continues by being run again.

**What is not in this repository.** SHAP was not computed. §6 allows it for
description only and forbids it from changing the verdict; with T1 failed on both
horizons there is nothing to describe but noise, and a SHAP plot of noise is the
artifact this study was designed to avoid producing. The liquidity filter dropped
CHGG (median daily dollar volume $1.2M) and ROOT ($17.0M) under the $20M rule
declared in §3 before any fit, leaving 68 names; RDFN returned an empty payload
and is absent from the panel, so 71 were requested and 70 stored.
