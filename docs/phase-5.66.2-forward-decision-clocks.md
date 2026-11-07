# Phase5.66.2 — forward arms, bounded parallelism and financial basis

Registered before semantic inference/forward decisions or new return joins.
Clarifies5.66 engineering constraints, not a choice after results.

## Actual financial capability and endpoint choice

The three registered AAPL probes all returned200, native count43→45/30000.
Financials439494B contains explicit annual/quarterly income/balance/cash/earnings
with currency. Earnings45886B contains157 rows. Fundamental-breakdown223826B
contains filing/general data, but its same2026-09-26 row mixes revenue114.477bn
with gross profit233.602bn/net_income132.175bn versus net_income_loss30.711bn.
Field duration/currency is insufficient to justify those ratios. It must not
become a margin/YoY signal by matching only form and end date.

Freeze live capture pair **financials + earnings**. Coherent report type, period,
currency and basis are required for metrics; otherwise unknown. Preserve the
actual probe and rejected mixed-period interpretation. No additional capability
request, data repair, retrospective first-known time or strategy result involved.

## Four-name timing and finalization

At most four newly admitted current-session candidates can arrive together.
Run at most4 concurrent per-candidate semantic calls and4 concurrent per-ticker
fundamental jobs (the two endpoints sequential inside each ticker). Global
daily budgets/durable unique claims still precede wire; concurrency cannot
multiply limits. Producer handles at most4 concurrent model calls, no arbitrary
backlog/retry. Unknown/refused/late work stays unknown. Test four simultaneous
45s model/16s financial jobs without serial late starvation.

A new fusion observer has PENDING versus immutable FINAL decisions. It must
not freeze an UNKNOWN merely because the first scan precedes both feature
workers. Finalize an arm once its required evidence is known, or classify its
missingness when the entry lead cutoff passes. Every used feature's physical
availability <= decision confirmation <= original entry target minus5s. Delayed
confirmation past cutoff makes the arm unknown, not a backdated decision. A
late final unknown may be recorded at its actual time but cannot be scored as
an entered position. Decision code never reads an entry/exit price/outcome to
choose its action. No result-dependent replacement, new slots or historical
semantic import can change the first four slots.

## Fixed forward arms

All arms share existing `shadow_price_v1` known-base slots and its independent
stock/SPY reference method. They are research decisions, not AL/SAT advice.
The operational profile fixes these four arms before first evaluation:

- **base**: original known-base flow/price candidate, ACCEPT before cutoff.
- **semantic_positive**: ACCEPT only if source-grounded direct-issuer confirmed
  positive interpretation, with no negative/mixed/contradictory item. A known
  empty headline packet or definite nonpositive interpretation is SKIP.
- **semantic_veto**: SKIP definite negative or mixed issuer interpretation;
  ACCEPT grounded confirmed positive/neutral interpretation or verified checked-
  empty packet. Rumour/expected/denied/unknown identity or interpretation is
  UNKNOWN, never a silently positive confirmation.
- **semantic_fundamental**: require semantic_positive ACCEPT plus coherent known
  annual/quarterly revenue YoY>=0 and operating margin>0. If either known metric
  fails, SKIP; missing/incomparable/conflicting values are UNKNOWN. These zero
  boundaries are explicit exploratory hypotheses, not learned weights.

UNKNOWN inputs remain UNKNOWN even if another component is favourable. For
conjunction, a known failed condition gives SKIP; otherwise any unknown blocks
ACCEPT. Raw provider sentiment and unrelated curated model opinions cannot
replace bound source interpretation. Neutral and no-news differ from unknown.
All outputs retain their evidence, reasons, model/profile/code/source identities
and clocks. No trading probability is fabricated.

Before-cutoff ACCEPT uses the existing paired entry/horizon stock references;
before-cutoff SKIP contributes explicit cash-zero to its preassigned slot only
when the required reference/arm universe is complete. UNKNOWN/late/missing
prices remain missing. Show15/60 separately and common complete days for paired
contrasts; incomplete days do not become a PnL sum over survivors. Inferential
readiness still requires80 complete future sessions. A short mirror stays a
separate non-executable diagnostic, not a fifth promoted trading arm.

Daily technical training/search never supplies fitted intraday news/financial
weights or probabilities. Its already-searched panel remains exploratory. The
new multimodal FeatureRow/OutcomeRow archive enables a later properly supported
fit; current live parameters and historical verdicts remain unchanged.
