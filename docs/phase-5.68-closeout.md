# Phase 5.68 — ThetaData option validation

**OPEN for live verification.** The product integration and connector are
implemented; real Theta Terminal access, current entitlements and production
deployment have not been verified in this cloud environment.

Owner authorization: explicit 12 November requests to integrate the paid ThetaData
subscription into UOA and investigate its useful capabilities. Working base was
updated to live `main` commit `0782f9bb047ff9aba2279e9ac0598de08caa80f3` before
the final repository checks.

## Delivered behavior

- Private `/opsiyon-dogrulama` page linked from the existing option screen.
- Recorded decision-card selection or explicitly retrospective manual requests;
  source identity is resolved server-side, never supplied as a claimed live fill.
- Standalone Python 3.9+ Mac connector using only the standard library. Existing
  authenticated local Theta Terminal stays private; UOA credentials go only to
  the configured site. No production database credential is needed on the Mac.
- Exact-contract native tick quote windows, preserved native timestamps, bounds,
  request hashes and raw JSON receipts; UOA recomputes one-contract costs and
  alternative 15/60-minute outcomes. Missing information remains UNKNOWN.
- Authenticated, same-origin writes, immutable requests/receipts, idempotent
  ingestion, bounded attempts and explicit owner-requested retry chains.
- Bounded dry-run-first capability probe and documented 34-endpoint options
  inventory. Professional labels in the source schema are not live account
  entitlement verification.

## Correctness findings

1. Historical UW chains were incorrectly considered complete after one 500-row
   response. AAPL 18 September produced 3,498 unique contracts across seven
   pages; 676 contracts in the 14–60 DTE band were absent from the first page.
   Current capture uses versioned pagination with cap/stall/conflict guards;
   the hash-pinned v1 collector, reports and archives remain unchanged.
2. Close-only daily stop exits substituted the stop threshold for the observed
   quote. The stored QQQ example changes from the unsupported −$47.20 to the
   observed daily-quote −$61.20 calculation. Historical artifacts are preserved
   and a regression records the discrepancy explicitly.
3. Eight previously recorded stock candidates were mapped to contracts before
   outcome lookup under a fixed retrospective audit protocol. Two +60-minute
   UW quotation outcomes were priceable (TSLA −$88.30, META −$228.30); six
   candidates and every +15-minute result remain unknown. No complete-cohort
   return, investment recommendation or profitability inference was made.

The chain defect is in the historical research path. Live dominant contracts
come from observed flow and explicit-symbol `/option-contracts` requests.
Missing ThetaData is not proven to cause the live engine's losses.

## Validation and judgments

Independent review covered native timezone conversion, quote provenance,
window/session/contract conflicts, near-close behavior, missing HTTP status,
nested continuation markers, corrected/canceled duplicate events, credentials,
timeouts and server-side arithmetic. Full repository checks are recorded in the
PR; source and test files were frozen for the final gate.

Final local gate: **7,197 passed, 52 skipped** in 335.65 seconds. The skips
include credential-gated provider tests; they are not live connectivity passes.
Strict mypy passed on 398 source files, ruff and the lockfile check passed, and
the staged credential scan was clean.

A real local HTTP end-to-end test ran the standalone connector against a
**synthetic** Theta responder and the actual FastAPI application, producing
three bounded reads and independently expected +$11.70/−$88.30 fixture results.
This proves implementation wiring, not provider access. Desktop and 390-pixel
mobile Chromium checks had no JavaScript errors or horizontal overflow.

Judgments:

1. The Mac connector is outbound-only because the owner has a working local
   Terminal; no public Terminal port, new hosting service or key transfer is
   necessary for this route.
2. One standard 100-share long contract is an explicit diagnostic assumption;
   adjusted deliverables remain unverified.
3. Composite native quote time is retained. Separate bid/ask update ages and
   trade execution timestamps are not invented.
4. A failed fetch currently makes the whole attempt unknown; it does not publish
   an incomplete successful-looking result. Explicit retries retain prior rows.
5. Capability documentation is a third-party copy because this environment's
   network policy denied the official docs host. No copied executable was used.
6. The new request and receipt tables are classified append-only in the existing
   whole-package durability guard. Existing tables are never reset or rewritten.

## Remaining work

The owner merges the reviewed PR under the repository's release rule. After the
new version is deployed, run the downloaded connector beside the already logged-in
Mac Terminal, request a known contract window and verify the received native
packet in UOA. Then execute the bounded capability probe and the fixed cohort
research in `thetadata-product-integration.md`. No real ThetaData access or
validated trading edge is claimed by this delivery.
