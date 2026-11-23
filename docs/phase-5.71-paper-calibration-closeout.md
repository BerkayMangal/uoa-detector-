# Phase 5.71 — Forward PAPER calibration closeout

The existing Live Alpha scanner feeds a new stock-only comparison. The four
arms use first-AL evidence, a confirmation guard, and fixed-target stop
alternatives. Each records both clocks, the cash risk at the reference
and at the modeled fill, sampled adverse and favorable moves, simulated
slippage, outcome status, and each applied decision.

The authenticated PAPER screen is linked from Results. It includes a ticker
filter, paginated cases, matched deltas, +15/+60-minute SPY markouts, and
per-case observation JSON. The screen and export read stored measurements.
The experiment has its own version, schema and cursor. Observations are
transactional. After interruption, the bounded recovery loop resumes
after the activation cursor from completed source snapshots and labels
recovered observations. Recovered scans use their own captured price observations and clocks.
The current quote cache is available only for the live scan.

Risk sizing rounds to whole shares and uses the saved reference and stop.
The two 1.5 ATR arms preserve the original levels and use the source share
count as a ceiling. All three confirmed arms use the same original price
entry range. Price receipts after a modeled fill are required before a
later stop or target can be hit. Positions missing their entire expiry
session remain unpriced.

Unit and integration validation covers version and activation isolation,
replayed scan order, future or stale receipts, WATCH at fill, delayed
confirmation, same-target risk sizing, expiry and missing exits, matched
benchmark windows, and authorized read-only HTML and JSON. The source
strategy threshold and brokerage boundary remain governed by their
existing contracts.

A constructed stock opportunity exercised the actual Live Alpha worker
and all four arms through entry, divergent stop outcomes and both benchmark
windows. The same synthetic ledger passed with SQLite and PostgreSQL 16.
No synthetic outcomes enter production or the independent archive analysis.
