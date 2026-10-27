# Phase 5.33 — position management, risk, benchmarks and scenarios

Frozen before code, 2026-10-27. The owner approves all five desk improvements
and continued deployment in chat. No broker connection or order execution.

## 1. Position desk

Private `/pozisyonlar`, with a concise home summary, separates PAPER simulation
from journal-recorded real trades. Show status, current sampled underlying,
mark age, plan stop/target, observed mark P&L when available and exit reason.
Unpriced/stale marks remain unknown. Pending PAPER is not funded exposure.

Portfolio numbers are scoped to recorded positions, never the owner's entire
broker account. PAPER uses its labelled simulation capital. Sum funded entry
outlay and planned stock stop risk or paid debit plus model fees for long/debit
options. Stop risk is not a guaranteed maximum loss. Show unknown-risk counts
and concentration by the existing committed peer groups; ungrouped names stay
ungrouped. Never pool manual and PAPER totals or treat correlated bets as
independent. No new statistical confidence score or automatic risk-based order.

Journal trades may attach an append-only owner-written UNDERLYING stop/target
plan through an authenticated same-origin POST. Validate positive finite levels,
existing open trade and direction ordering. Preserve old plans and existing
journal fills; a plan/alert never closes a real trade or invents a broker fill.

## 2. Exit alerts

Extend opt-in browser alerts to recent immutable PAPER exit/exit-pending events
and newly observed manual-plan level crossings. Label them PAPER/manual and
"exit review" with their recorded reason. Entry scope remains CALL-only or
all BUY; a separate enabled exit preference covers recorded positions, including
puts. No automatic sale. Exit notices can arrive after regular session close
when the tracker records them. They expire after 1800 seconds, retain exact
event time and are deduplicated by position/plan and event reason. Keep the
existing browser-state atomic claims, permission/page-open limits and first-enable
baseline. Price-unknown exits are explicitly labelled, not filled at zero.

## 3. Prospective matched references

New append-only observations/benchmark snapshots only; no old-schema alteration.
At new PAPER entry and completed priced exit, capture the price context actually
available to that cycle, SPY and entry-frozen peer membership. Record event,
availability/source times, same-session checks, missingness and configuration
hash. References use cached sampled prices, not tick-synchronous executions;
each price must be <=900 seconds old and paired source times <=60 seconds apart.
Keep that approximation visible. No backfill of old fills or repair with future
quotes. Every required fixed peer must be priced at both endpoints or its
comparison is unknown; do not drop losing/missing peers after entry.

Report directional UNDERLYING return minus matched SPY/peer return, percentage
points before costs, next to the existing model-net PAPER dollars. This is not
an option-return alpha, executed hedge or significance verdict. Reporting includes
all closed records in coverage; missing entry snapshots remain missing.
Capture/monitor failures cannot fail the legacy cycle. Bounded off-loop hooks,
no additional vendor requests, safe status metadata only in public health.

## 4. Option scenarios

Retain optional last-transaction IV and its tape timestamp from existing contract
responses, without changing instrument preference or eligibility. Use it only
when valid, nonfuture and <=900 seconds old. Otherwise, an explicitly labelled
mid-quote-implied MODEL IV may be solved inside configured bounds; impossible
prices yield unavailable scenarios, not an invented volatility.

Scenario grid is fixed in a NEW profile: underlying moves -2/0/+2%, IV changes
-5/0/+5 volatility points, horizons 1/3/5 declared sessions. No probabilities or
targets inferred. European Black-Scholes is a labelled approximation for US
options, risk-free 4% and dividend yield zero are stated assumptions, early
exercise/dividend/event jumps are not modelled. Use the current half-spread as
a fixed future spread assumption, existing entry-side/latency/commission model
and whole affordable quantities. Show three baseline cases by default and the
full grid in a fold. Compare stock and existing constructed structures under
the same explicit budget; these are hypothetical marks, not future NBBO/fills.

## 5. Delivery and tests

New operational settings in `desk_v1.yaml`: cache/alert limits, labelled PAPER
capital 10000, concentration warning 50% and planned-risk warning 5% are display
design choices only, not fitted trading gates. Frozen old profiles unchanged.
Authenticate/noindex all new routes/assets; public SEO stays unchanged. Keep
charcoal/copper layout, explicit entry/wait/exit/data states and collapsed details.

Meaningful tests cover finite-number/direction validation, pending versus funded,
stock/option quantities and fees, missing marks, scope separation/concentration,
manual plan revisions and no actual closure, exit de-duplication and closed
session delivery, exact as-of/missing reference behavior and fixed peers,
Black-Scholes known values/IV inversion/expiry and unavailable IV, cost arithmetic,
failure isolation, auth/CSRF, browser forms/alerts/refresh/mobile/accessibility.
Full repo gate, green CI, PR merge and verified Railway SHA before announcing
release. Empirical alpha assessment continues separately in phase 5.34.
