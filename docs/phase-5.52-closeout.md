# Phase 5.52 closeout — data readiness and call progress

CODE COMPLETE; native repair and exact release evidence are recorded separately
in the PR after checked deployment. No calibrated alpha, live threshold change,
order execution or option P&L is introduced by this phase.

## Delivery

Phase 5.52.1 adds a bounded preview/apply current-data command and a native
one-shot transport for this executor's HTTPS-only networking. Morning coverage
was 35/82; the 47 missing/stale research names, including all sector ETFs, are
eligible for repair. A request retains at most 60 completed sessions per name;
it does not supply the missing causal historical flow evidence or relax the
80-session event-calibration gate. Old Study G files and labels are not used.

The native transport is disabled unless an explicit DAILY_REPAIR_REQUEST id is
set. Its database claim wins atomically once across pods/redeploys before HTTP.
Crash or stopped results never retry automatically. Existing prices, first
availability receipts, cohorts, outcomes and job markers are never overwritten.
New bars are physically stamped after each response, then hash-receipted after
commit. Invalid, conflicting or gapped native data stay unavailable. Operator
budgets are 82 calls, >=5000 native daily headroom and >=.75-second spacing;
auth/quota/transport/HTTP errors stop without retries. No scheduled daily-job
cadence or vendor loop changes.

A new private append-only alfa_daily_repair_run table retains claim, compressed
sealed source evidence and final manifest. The table is registered in the
existing P37 durability classification; every prior test assertion remains
unchanged, and new receipt evidence gains the same protection. This is the only
existing-test edit. No credential, connection URL, raw vendor packet or SQL is
published. The private GET maintenance status exposes only safe result counts.

The call list now uses compact responsive records with +15/+60-minute underlying
returns, matched SPY difference, first-entry ownership and distinct pending,
awaiting, missing, partial, invalid and unknown-calendar states. Source snapshot,
card and current opening profile are checked; timestamps must predate the call,
and outcomes must match the existing exact target/availability semantics.
Updates never inherit a first entry's return. Display/filter counts are not a
global strategy sample or a win probability. The detail page puts all horizon
results first and folds source/cost details while retaining original evidence.
These remain cost-before underlying references, separate from PAPER options.

Read-only adapters batch source/primary/outcome queries, bounded by the existing
profile. Duplicate displayed keys cannot inflate counts. Page requests do not
write, create tables, run repair, call vendors, optimize or create advice.
Original strategy/research/scorecard/forward/capture code and hashes stay fixed.

## Verification

23 explicit new cases cover:

- Closed-session boundaries, physical source/commit clocks, preview without
  tables, gapped/invalid/conflicting native payloads, immutable old keys and
  first receipts, idempotency, auth/budget stops and redacted storage errors.
- Original entry versus updates, exact/future targets, missing benchmark,
  source/profile tampering, duplicate scope, bounded batch SELECTs and private
  GET no-write/no-operator behavior.
- Concurrent native claims, restart exclusion, persisted private gzip source
  evidence, stopped/crashed attempts, empty-state reads and request-id validation.

Generic durability/model checks also exercise the new receipt model. Full gate
completed with **5,488 passed, 52 credential-gated skips, two existing warnings,
139.66 seconds**; strict mypy clean on 304 files, ruff clean, lock and diff clean.
Exact HEAD/PR/main CI and native counts are attached to the PR after completion.
Chromium at 1440/390/320 pixels exercised list/detail routes and folded costs:
200, no page overflow/browser error and zero axe violations in checked views.
Synthetic local calls/outcomes test the software only; they are not market
performance. Native coverage, source counts and actual call statuses must be
measured separately after execution, not inferred from those browser fixtures.

## Judgments and next evidence

1. Complete known missing current context before adding a new hypothesis.
2. A physically late historical price cannot become an original call feature.
3. Native transport instead of changing secrets/network controls for direct TCP.
4. One durable claim instead of an automatic retry after an ambiguous crash.
5. Missing observations remain unknown; no small-sample win-rate claim.
6. Add the new table to durability protection without weakening any old assertion.

Next: observe first recorded AL calls and their mature original horizon marks,
keep underlying references separate from instrument PAPER costs/outcomes, and
review prospective evidence under the existing frozen protocols. No automatic
promotion or search-until-positive tuning.
