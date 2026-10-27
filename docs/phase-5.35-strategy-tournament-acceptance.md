# Phase 5.35 — multi-family strategy tournament

Frozen 2026-10-27, before computing new strategy results. Owner explicitly asks
for additional variants, all available data/API coverage, calibration and live
deployment. This is a NEW bounded exploratory product experiment, not a rewrite
of calibration_v1, an old study, or a claim that historical reuse is fresh data.
No broker orders. No changes to frozen old profiles/acceptance docs/reports.

## Data and access

Historical prices: ONLY Study F's 70-name 252-session OHLCV panel. Historical
flow: ALL 133 Study E daily aggregates, 10 names, 2026-06-16..2026-09-18, only
as a separately labelled overlap diagnostic; absent rows are unknown. Option
replay snapshots/capability artifacts support an inventory, not a multi-year
option backtest. Synthetic/seed/pytest data and reserved Study G prices are NEVER
training or historical test data. Old study verdicts stay unchanged.

No UW/Theta credentials or production database credentials exist in this cloud
workspace; public historical Yahoo/Stooq endpoints were tried and returned proxy
403. Do not expose protected production data or credentials to work around this.
Live code may use existing observed UW alerts, module telemetry, news and settled
OHLCV already being collected by production. No additional vendor calls, cap
increases, paid services or mass downloads in this release. Record this access
matrix and actual coverage in the report, rather than claiming all APIs replayed.

## Fixed search

Six long-stock families, each with lookback {5,10,20}, four family thresholds,
volume gate {0,1,1.5} times prior-20 mean, SPY regime {any,above_ma20,below_ma20},
holding {1,3,5} sessions, sizing {equal_cash,atr_budget}, max names {2,4}.
6 x 3 x 4 x 3 x 3 x 3 x 2 x 2 = 7,776 unique configurations. Primary horizon
is part of the search and counted, not a hidden extra selection chance.

Features use valid, consecutive declared sessions, previous-14 mean true range,
prior-20 volume mean, and current close known after settlement. No future labels.

- relative_recovery: SPY-minus-own lookback return >= {0,1,2,4} pp;
  close >= open; daily close increase <=1 previous ATR. Rank greater lag first.
- relative_momentum: own-minus-SPY return >= {0,1,2,4} pp; close >= open;
  daily increase <=1 ATR. Rank greater relative momentum first.
- trend_pullback: close above previous lookback mean; previous-close minus
  current close / ATR >= {0,.25,.5,1}. Rank deeper pullback first.
- oversold_reversal: lookback-start close minus current close / ATR
  >= {0,1,2,4}; close >= open. Rank greater decline first.
- range_breakout: close minus prior lookback highest high / ATR
  >= {0,.25,.5,1}; daily increase <=2 ATR. Rank larger breakout first.
- volatility_compression: previous-5 mean true range / previous-20 mean true
  range <= {.4,.6,.8,1}; close > prior lookback highest high.
  Rank lower compression ratio first.

Tie by alphabetical ticker. SPY is reference only. Up/down stock shorts, synthetic
option P&L, daily-bar stop sequences and missing-data imputation are excluded.
Each portfolio starts with $10,000; max $2,500 entry outlay/name; whole shares,
no concurrent same-name entry, no leverage. ATR sizing also caps quantity by
$100/(1.5*previous ATR), sizing only, NOT a stop execution/max-loss guarantee.
Next session open entry, declared Nth session close exit. Opens consume cash
before that day's closes. No late-day re-entry from exit cash. Five bps adverse
slippage each side, no commission/tax; missing exits keep cash unresolved.

## Temporal selection, multiple search and diagnostics

Three expanding training ends: 2026-01-30, 2026-03-20, 2026-05-08.
Validation intervals: 2026-02-10..03-20, 03-31..05-08, 05-19..06-26.
Final internal test: 2026-07-08..09-18. Training starts 2025-09-18 with mandatory
warm-up. Exits must remain within their split; at least five omitted exchange
sessions between training/validation/test borders. All splits are reused data.

For each configuration: all three train and validation portfolios. Eligibility:
>=30 total training closes and aggregate training net and matched excess >0;
>=30 validation closes total, >=5 each fold, >=2/3 folds with BOTH positive net
and matched SPY excess, aggregate validation net and excess >0, no unresolved
positions/missing matched marks, worst validation drawdown <=20%.
Select ONE per family by median validation excess dollars/initial capital, then
mean validation excess, lower worst drawdown, lexical config ID. The overall
selection is highest on the SAME criterion across eligible family winners.
Freeze these choices BEFORE evaluating any final-test returns. Do not replace
an overall/family selection because another later test looks better.

Report all 7,776 grid rows, six splits each, every selected test and sensitivity,
source/profile/implementation/report hashes, selected ledgers and equity paths.
Test diagnostic gate: >=20 closes, positive net and matched excess, no missing/
unresolved prices, max drawdown <=20%; pass permits EXPERIMENTAL PAPER only.
Sensitivity: selected test at 10/20 bps per side, counts explicit, no retuning.

Joint bootstrap audit: 200 seeded replicates, five-session moving blocks sampled
within EACH validation fold, same sampled dates for all configurations. Center
each strategy's daily matched-excess increments; for every replicate take the
maximum mean across ALL configurations. Compare each selected observed validation
mean to that best-of-grid null, report finite-sample p=(1+exceedances)/201 and
95th percentile. No independent-trade t-test. This conditional, reused-data
search audit is NOT an untouched alpha confirmation, regardless of p-value.

Flow overlap diagnostic: selected candidates tested only on the final internal
test interval, with a known Study E row, comparing eligible known-row signals
versus known-row mean_score>=.4 and print_count>=1. Missing rows stay unknown;
no sample means unavailable, not zero. Different tape/cached score semantics from
new corrected live side-premium flow remain explicit. No further optimization.

## Prospective API confluence and product

Separate `/stratejiler` private report with family standings, fixed overall
selection, robustness/coverage limits, latest experimental candidates and future
PAPER outcomes. Concise links from home and Alfa lab, no green live BUY-policy
replacement. Show clear AWAIT DATA / PAPER CANDIDATE / REJECTED states. Preserve
existing portfolios, manual trades and the calibration_v1 candidate.

New signals start 2026-10-28 ET; instruction must be written before the exact
next regular open. Per family/arm/ticker/day append-only identity. Top max_names
qualifying names per family/day by the frozen ranking. Independent $2,500 PAPER
unit budgets, optionally ATR sizing as frozen; NOT the historical cash-limited
portfolio. Record exact future entry/open and horizon/close only after settlement,
whole quantities, costs, matched SPY dates, pending/unfilled/unknown coverage.
Never reconstruct a past missed instruction or substitute a later day's price.

At the FIRST candidate observation freeze as-of API context and availability:
corrected observed UW flow, gamma M21, calendar M22, price M23, IV M24, peers M25,
dark-pool M26, OI M27 and news cache. Require same signal-session source times,
<=4h old, written_at<=instruction; degraded/preset/no-data/timeout never confirms.
Raw cached telemetry/branch does not establish predictive validity. OI observed
after entry cannot confirm a past instruction. News failures/unknown are explicit.
Six fixed prospective arms: price-only; flow; flow+gamma; flow+dark-pool; flow+OI;
full context (flow, gamma, dark-pool, OI, peer alignment, non-exhausted IV,
non-blackout calendar, successful news check). Flow uses the existing corrected
ask/bid alert accounting with a new four-hour view, explicit capture limitations.
Never pretend it is the historical Study E score or a lossless exchange tape.
All arm assignments/context freeze on first observation; later data cannot repair
old membership. API arms are hypotheses, not historically calibrated winners.

Monitoring is off-loop, timeout/single-flight/rate protected, cached-price only;
failures cannot stop the live desk. New additive append-only tables only; no
old schema/profile modification. Private Basic auth/noindex/no-store, public
health safe status/counts only. User-visible times Europe/Istanbul, audit UTC.

## Delivery

Meaningful tests: prefix causality, threshold families, calendar gaps, cash/size/
fees, unresolved exits, rank/selection independent of final test, joint bootstrap
sampling, missing historical flow, frozen as-of API context/degradation, no late
or duplicate PAPER instructions, price endpoints/coverage, auth and isolation.
Browser desktop/mobile and accessibility checks. Full hermetic gate before each
commit, green CI, authorized PR merge, verified Railway SHA/job status; report
actual counts/results and access limits. No search-until-positive loop.
