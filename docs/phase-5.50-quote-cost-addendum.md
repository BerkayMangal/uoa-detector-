# Phase 5.50 — descriptive archived quote-cost audit

Frozen before inspecting cost results, 2026-10-31. The eight existing trimmed
chain files have been inventoried by schema only. This adds a descriptive cost
calculation, not another strategy hypothesis, grid, promotion or option backtest.

Read exactly the registered replay/chain*.json files. Deduplicate by session,
ticker and option_symbol; conflicting quotes are excluded, never chosen by cost.
Use calls with 14–45 calendar DTE and positive uncrossed recorded NBBO. Report
all calls and a preregistered abs-delta .25–.75 stratum; missing delta is unknown.
Also enumerate one adjacent-strike call debit vertical per lower strike within
each original session/ticker/expiry, using only the same chain snapshot. Only
positive debit below strike width survives; no strike optimization or winner.

Profile event_quote_audit_v1 pins the current 25% cost/debit reference gate,
USD100 one-unit budget, USD0.65 commission/contract/side and assumed standard
100-share multiplier. Long entry uses ask, exit bid; short is reverse, fees
charged once on entry and once on exit. Report counts, cost percentiles and
how many individual structures fit budget/cost gates in each stratum. These
are overlapping quote combinations, not independent samples or selected trades.

NBBO is receipt/session-dated quality B with unknown exchange/concurrency times.
The multiplier and corporate-action adjustments are assumptions, not verified
for every contract. Do not invent execution, future option returns, success
probabilities or an optimal cost cutoff. Keep the 438 event-calibration grid
unchanged and its zero real market replay count separate. Persist source and
script/profile hashes plus the complete descriptive ledger for reconciliation.
