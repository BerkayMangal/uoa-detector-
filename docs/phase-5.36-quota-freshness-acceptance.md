# Phase 5.36 — quota observation freshness

Frozen before code, 2026-10-27. Owner requests continuing work with all available
APIs and deployment. A local reproduction demonstrates that an old client header
count of 24,000 is folded into a NEW empty ET-day reservation row, denying all
requests even though no response has measured the new day. This is not evidence
that today's production quota warning is false; fresh counts must still cap work.

Add native read-only last-valid-count observation time (UTC), stamped only with
a valid nonnegative count header, including valid error-response headers. Missing/
malformed headers must not refresh either count or timestamp. Do not alter the
existing count property's meaning or retry/circuit-breaker behaviour.

Reservation consumers (Live Alpha Budget and options PAPER historic fetches)
accept a native counter only when its observation belongs to the requested ET
ledger day and is not future to the reservation. Old clients/fakes without the
optional timestamp retain their conservative legacy count semantics. Bad/future
or stale explicit timestamps cannot import a cross-day counter. Existing atomic
reserve/monotonic floor semantics, actual same-day caps, strategy profiles and
historical report hashes remain unchanged. No quota increase, forced reset,
credential access, new vendor requests, or repair of old evidence. The next real
response measures the new day's count; actual high counts still stop work.

Tests cover stale/new-day refusal reproduction, valid same-day high counts,
future timestamps, ET boundary vs UTC midnight, zero/malformed/missing/error
headers and both reservation consumers. Full existing hermetic gate; strict mypy,
ruff/lock; green authorized PR merge and matching Railway SHA. Report the exact
fix and remaining genuine quota/access limits, without claiming new history.
