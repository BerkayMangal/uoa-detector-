# Phase5.67 — settled-data coverage guardian and bound news diagnostics

Registered2026-11-10 before implementation. Owner continues the authorized UOA
measurement/calibration system and asks current status. Nov9 real receipts show
2eligible candidates,8timely decisions and all entry/15/60 price references;
three semantic arms remainUNKNOWN. No fitted weight or strategy change follows
from those returns. Nov10 preopen settled daily archive again expectsNov9 but
hasNov6/0of70current, despite yesterday's bounded manual repair. Existing daily
job SUCCESS does not prove coverage, and its same-day scheduler does not repair
previous-day gaps. Exact Monfailure subcause is not established. This release
addresses the observed coverage gap without claiming a proven underlying cause.

## Independent bounded maintenance

Add an isolated versioned guardian, disabled unless explicitly enabled. On a
verified regular trading day, within the fixed05:00–09:15America/New_York window,
read current settled-day coverage using existing verified receipts. Unknown
calendar, limited/unverified coverage, unavailable storage or out-of-window time
must cause no provider request. Fully covered data means zero vendor calls.

If current settled coverage is missing, invoke the EXISTING daily_repair/operator
once with a deterministic requestID keyed by guardian-v1 and expected settled
session. Its durable atomic claimed record must precede any wire call across
restarts/replicas. At most one such run/session, no retry of partial, failed,
orphaned or unknown attempts. Detect an already running manual repair and wait;
do not race it. Keep the unchanged daily_repair_v1 limits:82requests maximum,
5000quotareserve,0.75sspacing,30s timeout,60completed-session operational history.
Reject drift from these limits rather than silently expand them. Only missing
members are fetched; the original operator stops on auth/quota/unknown failure.
No extra subscription, capacity increase, database migration or broker action.

The original one-shot interface remains one-shot; this independently registered
scheduler is the new authorization boundary for bounded maintenance. Existing
rows, markers, outcomes and first receipts remain immutable. Conflicting vendor
revisions stay flagged, never overwritten. New rows/receipts carry actual current
response/commit clocks, not historical first-knowability. Current maintenance
history cannot be used to refit, fill past decision inputs or inspect StudyG.
Keep it separate from frozen pre-September18 strategy research.

Use a separate failure-isolated service thread/engine with bounded PostgreSQL
pool/timeouts, following the existing isolation pattern. Status and authenticated
GET projection expose only safe coverage/ownership/result counts, never raw
packets or credentials. GET never starts work, writes or creates tables.

## Read-only bound news-quality diagnostics

Add a helper OUTSIDE frozen source/semantic/fusion implementation identities and
an authenticated bounded GET (up to4 original candidates per selected session).
Run unchanged original source verifiers before inspecting the candidate-bound
packet. Check packet identity/hash/ticker, bounded gzip/decompressedsize8MiB,
source clocks and exact original profile identity. Report only safe metadata:
packet item count, configured requestedlimit, pagination/limited flag booleans,
physical availability, application check time, and source-evidence reasons at
original scan-finish versus candidate-publication clocks. No headlines, body,
URL values, secrets, model call, evaluation, inference or repair on GET.

Missing/truncated/expired/unmatched data stayUNKNOWN with explicit diagnostic
codes. This may explain MSFT's original UNKNOWN, but never changes that stored
record, checked-empty vote or a model/gate. Preserve old capture/profile/verifier
hashes and all four PAPER rules. Presentation-only diagnostic fields may be
added; they must never flow into trading policy. Existing histories/activations
and first-day stored aggregate remain in their original cohort.

## Delivery proof

Test no-op full coverage, calendar/window guards, quota/config drift, one claim
across restarts/concurrency, manual-inflight hold, failure/partial/no-retry,
physical clocks and SELECT-only status. Test source-bound diagnostics including
full stale-headline page, pagination, age-boundary differences, malformed/gzip
limits, hash mismatch, missing packets and private response/auth boundaries.
Preserve existing assertions; full unchanged hermetic pytest/mypy/Ruff/lock before
commit; exact-green feature and main CI, normal matching-head manual merge,
exact Railway SHA/SUCCESS and healthy source/guardian, actual coverage/result
and Nov9 original decisions/price reference parity before completion claims.
No strategy calibration/promotion, probability claim or old PnL rewrite.
