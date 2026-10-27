# Phase 5.40 — failed-cycle diagnosis and bounded recovery

Frozen before implementation, 2026-10-28 Istanbul. During Phase 5.39 work,
production c08000f reached Railway SUCCESS/matching SHA but its first live-alpha
cycle FAILED, with null mode/cards and no monitor statuses. Existing public
health does not reveal even the error category or source location. The closed
loop then sleeps 1,800 seconds, leaving a startup failure unexamined for 30min.
The cause is UNKNOWN; do not invent or claim a repair before observing evidence.

Owner authorizes continuing reliability improvements and deployment. Add safe
failure diagnostics and recovery timing, without altering scores, thresholds,
PAPER sizing/fills, old reports, profiles, quota limits or transaction semantics.

## Safe diagnostics

Track the attempted stage: scan, shadow, desk, calibration, tournament; readiness
retains its independent Phase 5.39 fault isolation. On failure publish ONLY:

- a category from a fixed allow-list (storage/missing-table/busy/conflict,
  quota/auth/network/timeout, invalid-data/runtime/internal as appropriate);
- a known exception class from an allow-list, or generic Exception;
- the last project-owned code frame, relative path plus line number, or absent.

Never publish exception messages, SQL, HTTP responses, URLs, absolute runtime
paths, vendor payloads, function arguments, locals, credentials or arbitrary
user-controlled strings. Only project-owned webapp/src-uoa files qualify as a
location. Keep existing private logs/heartbeat detail; public health receives the
sanitized structure ONLY for a failed cycle. Successful later cycles clear it.
This is diagnosis, not a claim that the underlying cause has already been fixed.

## Recovery

After a FAILED cycle outside regular/premarket hours, retry on the EXISTING live
cadence (300s under the frozen profile), rather than closed cadence (1800s).
Use min(existing live cadence, existing closed cadence); no new numeric tuning.
Healthy/degraded closed cycles retain their existing cadence. Live and premarket
cadence is unchanged. Existing daily atomic quota reservation, deduplication and
PAPER idempotency still apply; no fresh endpoints, cap increases or order routing.

No changes to archived tournament/calibration/stress packages, profiles, artifacts
or forward_v2 runtime. No strategy promotion, retrospective retuning, new vendor
credentials or protected database access. Do not equate HTTP 200 with recovered
signal processing. Verify matching final SHA and a subsequent successful/degraded
scan with actual desk/research statuses before calling the pipeline recovered.

Tests: sanitization of secret-bearing messages/SQL, known/unknown exceptions,
project/external code frames, exact failure stage, failed/success cadence at
closed/live/premarket states, cleanup of old failure after recovery, continuing
independent readiness. Full hermetic pytest, strict mypy src/webapp, ruff, lock
and diff before commit. Both phases may be delivered in one green PR, preserving
each independently green commit. Green main CI and Railway SHA/runtime evidence
required; persist on any diagnosed concrete defect, with a new frozen fix scope.
