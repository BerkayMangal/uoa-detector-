# Phase 5.34 — bounded exploratory calibration

Frozen 2026-10-27 BEFORE candidate returns are calculated. Owner explicitly
requests implementation, calibration, prospective PAPER measurement and deployment.
No frozen strategy, acceptance document or old study verdict is retuned.

## Scope and evidence

Use ONLY `data/study_f/bars.csv` for the historical search. This 252-session,
70-name panel has already been searched in Study F: every historical result,
including the internal test, is EXPLORATORY ONLY. Survivorship, corporate-action
adjustment quality, overlapping positions and selection effects remain limits.
Never load Study G prices or call the historical split an untouched experiment.
No options history or IV data is inferred from daily stock bars.

Create a NEW immutable calibration profile with 1728 configurations:
lookback {5,10,20}, minimum lag behind SPY {0,1,2,4} percentage points,
minimum close-minus-open recovery / previous 14-session mean true range
{0,.25,.5,1}, minimum current volume / previous 20-session mean
{0,1,1.5,2}, SPY regime {any,positive,above_ma20}, maximum close-minus-previous-close
/ previous ATR {.5,1,2}. Lag/recovery use only data available at that close.
Require lag >= minimum, recovery >= minimum, chase <= maximum; positive prices,
valid OHLC, consecutive declared exchange sessions and positive prior ATR.
SPY positive means its lookback return >0; MA20 includes the signal close.
Volume threshold zero means no volume gate; other thresholds require valid data.
SPY is reference only; all other covered names remain in the fixed universe.

Primary holding period is 3 sessions; 1 and 5 are secondary diagnostics of the
single selected candidate only, never alternate selection chances. Count every
trial, selected-candidate horizon and sensitivity evaluation in the manifest.
No billions-of-variants claim and no search-until-positive loop.

## Chronological selection and accounting

Training signals through 2026-03-19, validation signals from 2026-03-30 through
2026-06-18, internal test from 2026-06-30 through the panel's final date. Reject
trades whose final exit exceeds its split. These boundaries and five-session
purges are fixed from panel date coverage, before seeing candidate results.
Warm-up uses earlier bars, never future features or labels.

Each trial is a separate $10,000 cash-only portfolio: maximum $2,500 entry outlay
per name, maximum 8 positions, whole shares, at most one active position per
name, no leverage or overlapping re-entry. After signal close, entry next regular
session open; exit at the 3rd regular session close including the entry session.
Entries consume cash before that day's closes can release it. Simultaneous
candidates rank by descending lag then alphabetic ticker, fixed ex ante. Entry
and exit each assume 5bps adverse slippage, no commission/tax. No daily-bar stop
execution or fabricated intraday sequence. Missing outcome prices keep an open
unresolved allocation, never silently drop a loss or reinvest unavailable cash.

Train eligibility: >=30 closed trades, positive net dollars and positive mean
matched SPY excess. Validation eligibility: same requirements plus maximum equity
drawdown <=20%. Choose highest validation mean SPY excess, then lower drawdown,
then lexical configuration ID. If none qualifies, freeze NO_CANDIDATE.
Evaluate the single selection once on internal test; fail the same count/net/
excess/drawdown gates => REJECTED and no prospective promotion. Surviving selection
is only EXPERIMENTAL, never measured alpha. Record full grid scores, each stage's
counts, costs, coverage, chosen ID, data/profile/code hashes and deterministic
trade ledger. Read-only private report displays selection and failures honestly.

## Prospective monitoring

If a candidate survives, freeze it with its hashes before 2026-10-28 ET. Future
cohorts start with signals on/after that date. Capture settled OHLCV from the
already-requested daily-close payload in a NEW append-only table; no additional
vendor calls and no changes to legacy OHLC schemas. Fixed universe coverage is
reported; an uncollected name is missing, not a failed/zero-return signal.
Late observations cannot create historical fills: a decision must be saved before
its entry session opens. Require the exact next session's open and exact horizon
close, observed only after each settles. Keep pending/unresolved records visible,
freeze all decision inputs and prices on first observation, never route orders.
Evaluate per-candidate PAPER unit budgets with explicit independent-record scope,
not as the historical cash-limited portfolio. Matched SPY with the same dates,
model-net dollars, closed/pending/unknown coverage. No automatic promotion.
If no candidate survives, the runner and report still ship and explicitly show
no new calibration PAPER; existing live PAPER and alpha-lab experiments continue.

## Delivery

Private calibration report linked from Alfa lab, Basic auth/noindex/cache rules
unchanged. CPU-heavy search is an explicit offline CLI, never an HTTP-triggered
optimizer. Live monitoring is bounded and failure-isolated. Tests cover prefix
causality, exact calendars/gaps, split purges, budgets/quantities/costs, cash order,
selection independence from test, rejects, append-only/as-of capture and no late
fills, privacy and browser accessibility. Full hermetic gate, green CI, merged PR
and verified Railway release. Actual run artifacts and a closeout record limits
and the result without changing the gates after seeing it.
