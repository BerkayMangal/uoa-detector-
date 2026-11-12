# Phase 5.70 — Automatic Theta measurement closeout

Implementation complete; release candidate verified on 12 November 2026.
Production and actual Mac/Theta verification are separate boundaries; this
report does not assert deployment or live provider access.

## What changed

- The existing opening journal's first BUY per ticker/session supplies the
  exact saved single-long-option plan. A bounded observer admits it once,
  resumes interrupted request creation, and records unsupported plans as
  exclusions. It does not promote candidates or change a strategy.
- After three nonfinalized failed attempts, an explicit owner retry can create
  a linked request (maximum eight retries). The original admission, contract
  and target remain fixed; every ancestor and receipt stays in the export.
  Reports follow the active descendant. A finalized priced or UNKNOWN outcome
  cannot be retried to pick a more favorable result.
- The observer starts with seven calendar days of explicitly retrospective
  catch-up, then uses the first observation of committed sources plus 60 seconds
  for new targets. Publication timestamps are not claimed to prove historical
  physical availability. A source plan that expires before the selected native
  entry quote cannot produce a priced result.
- Explicit continuous device enrollment works with zero pending jobs. The
  one-use link lasts 24 hours; a scoped device can handle future validation
  jobs until the owner revokes it. Old two-hour/seven-day/frozen-roster pairing
  behavior remains intact.
- A one-time Mac user installation restarts on login and reconnects after
  outages. Health heartbeats, Terminal self-report and validated quote receipt
  remain separate. Native payloads replay byte-for-byte after an ambiguous
  acknowledgment. A definite quote-validation refusal retains the original
  bytes and records a truthful UNKNOWN attempt, allowing the bounded queue to
  progress. The device cannot read owner reports or administer the site.
- **Otomatik takip** now leads with connection state, automatic admissions,
  measurement coverage and results. Manual historical forms are secondary.
  Read-only status refreshes every 15 seconds. Per-day reports recompute saved
  native packets with the same costs and display both horizons and missingness;
  source/receipt/method identities support repeatability. There is no optimizer,
  automatic trade, or new strategy profitability claim.
- Phase 5.70.1 corrects the production ATM refresher's acquisition timestamp:
  each decoded ticker response gets its own completion clock. Provider dates
  and observation times, selection rules and freshness thresholds stay intact.

## Verification

The complete hermetic release gate passed: **7,454 passed, 52 skipped** in
326.91 seconds; strict mypy passed across 403 source files, ruff passed,
`uv lock --check` passed and `git diff --check` was clean. This is a net
increase of 197 passing cases over the Phase 5.68.2 baseline. Key-gated
provider integration tests were skipped, not treated as provider verification.
The two existing warning categories were Starlette/httpx deprecation and a
gamma interpolation runtime warning.

Focused device, legacy compatibility, app-authentication, reconnect/installer,
UI, native packet and ATM receipt-clock suites passed during development.
The full gate initially caught the new navigation/public-route registrations
and an unnecessary public-health field; the registrations were updated and
the health field removed. The affected 980-case suite and full gate then passed.
The local real-HTTP synthetic test enrolled a device with no jobs, added a
later request, received three native-format quote windows, computed +$11.70
and −$88.30 independently on the server, emptied the acknowledged outbox and
denied subsequent access after revocation. These numbers are synthetic test
fixtures, not market research or live Theta verification.

Browser checks at 1440px and 390px verified no horizontal overflow or JavaScript
errors, four read-only polling requests, cleared setup fragments, a downloaded
filename matching its installation command and revocation of the unstarted
test device. Populated measurement views were also checked.
Independent final review found no additional release blocker in source
admission, replay accounting, capability isolation or application wiring.

Production preflight at 20:14–20:15 UTC still showed Phase 5.68.2 (`807a307`),
Railway SUCCESS and a working scanner. Theta had no connector contact or
validated results; eight legacy jobs remained pending. There were no eligible
saved option sources and no measurement rows at that observation. The new
automatic routes were not deployed at preflight. No fake quote or heartbeat
was sent to production.

## Judgment calls and practical limits

1. Existing opening-primary identity is preserved. The first plan can be stock
   or a spread; a later single-leg plan is not substituted after observing its
   result. Thus working scanners and many candidate cards can still yield no
   eligible automatic option entry.
2. Prospective and retrospective entry clocks remain separate report groups.
   Both are one-contract hypothetical quote markouts. Alternative +15/+60
   outcomes are not added together or called account profit. Unknown is not zero.
3. The device credential is revocable and restricted to this job pipeline.
   Server storage keeps hashes only. Installed Mac files are mode0600 in a
   mode0700 app directory; browser download permissions cannot be controlled
   before first execution. The installed script removes its embedded token and
   reads the private configuration. No Terminal port is exposed publicly.
4. The standalone connector duplicates the frozen native v1 helpers so a
   downloaded file needs no repository or third-party package. AST parity and
   legacy tests guard that deliberate duplication. Python 3.9+ is still needed.
   Real macOS `launchctl` installation has not run in this Linux workspace.
5. Pending plus retained native bodies are capped at 64 MiB, with at most
   32 retained definite refusals. Exhaustion requires review; evidence is never
   silently deleted to keep the interface green. Provider entitlement cannot
   be proved by an open TCP port or a heartbeat.
6. Cohort exports and replay have explicit row/byte bounds. Metadata reads
   avoid loading raw packet bodies, and unchanged input snapshots reuse the
   existing immutable report. Large reads fail explicitly rather than silently
   report a subset as complete.
   Startup table preparation retries transient database failures; the observer
   uses its own bounded database pool and statement/lock timeouts.
7. The ATM clock defect can artificially age a fresh response; it is not proof
   of the cause of all WAIT decisions or prior losses. A distinct daily-close
   gap remains possible for newly followed tickers or partially failed
   post-close batches. This repair does not rewrite daily history or relax
   freshness to hide missing data.
8. Existing test registries explicitly add the new public capability-handoff
   routes, durable tables and navigation destination. All previous private
   routes, durable evidence and fifteen menu destinations remain covered.
   New real-app tests separately enforce the handoff grant and exact method/path
   boundaries. Observer diagnostics stay behind owner auth; the public health
   response does not gain a new automatic-observer field.

## Next verification

After merge/deployment, verify the actual deployed SHA, owner/device route
boundaries, observer state and enrollment download. Actual Mac startup and the
first real validated Theta packet remain necessary to close provider access.
Phase 5.69 research PR #150 remains separate; no rule from it is promoted here.
