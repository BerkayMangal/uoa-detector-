# Phase 5.68 — ThetaData option validation in UOA

Authorized by the owner's explicit requests on 12 November 2026 to integrate
his ThetaData Options Pro subscription into the existing product and investigate
its capabilities. This is a new measurement feature, not a change to any frozen
strategy or an assertion of predictive advantage.

## Product behavior

1. An authenticated UOA user selects an existing recorded option decision, or
   enters a clearly labelled retrospective contract/time request.
2. A bounded connector running beside the user's already authenticated Theta
   Terminal reads pending requests from UOA over HTTPS. No public Terminal port
   and no production database credential are required on the Mac.
3. It requests tick NBBO observations for one exact contract and short windows
   around entry and alternative 15/60 minute exits. The server verifies contract,
   session, window, receipt time and immutable request identity, then recomputes
   the economics. Client-supplied PnL is never accepted as evidence.
4. The existing product gains an authenticated option-validation page with
   source, contract, quotation times, modeled costs, outcomes and missing-data
   reasons. No GET triggers vendor requests. Missing Terminal access is visible
   as awaiting connection, never a fabricated successful integration.

## Fixed diagnostic assumptions

One standard 100-share contract, long CALL or PUT; entry ask plus $0.01/share,
exit bid minus $0.01/share (floored at zero), $0.65 commission per side.
First valid observation at/after each target within 120 seconds. Horizons are
measured from the actual selected entry observation and are alternatives.
Standard deliverables are assumed and unverified; adjusted contracts are not
silently treated as standard. Calendar coverage is mandatory.

Theta quote timestamps describe native quote observations. They are not
fabricated trade times or separate bid/ask update timestamps. Later downloaded
history measures retrospective quotation economics, not what the application
physically knew then and not guaranteed broker execution.

## Scope and evidence

The connection supports a Mac Terminal today and the same connector beside a
privately hosted Terminal later. Keys stay out of source, responses, logs and
the product UI. Hosting, provider credentials and fresh provider entitlements
must be verified before claiming live ThetaData access.

Existing signal policies, historical artifacts and old PAPER outcomes are
preserved. Separately discovered correctness fixes are documented: observed
daily stop marks cannot be replaced by better unobserved stop fills; current
UW chain capture must paginate instead of labeling 500 rows complete.

Tests cover arithmetic, absent data, window/session/identity conflicts, native
timestamp conversion, bounded requests, idempotent ingestion, authentication,
and server-side recomputation. The full repository gate runs before a commit.
The owner merges any PR; no automatic main merge or production deployment.

## Completion boundary

Code and an end-to-end mock test establish implementation only. A real Theta
response through the configured Terminal, authenticated ingestion and rendered
result establish integration. Research conclusions require their own fixed
cohort, coverage and costs; no profitability claim follows from adding a source.
