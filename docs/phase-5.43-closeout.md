# Phase 5.43 closeout — bound PostgreSQL dynamic-memory demand

KAPALI — app PostgreSQL queries use serial execution; safe capacity hints now
distinguish explicit shared-memory, temporary-storage and disk messages. Actual
production recovery remains to be observed, not inferred from a green build.

## Observed incident and mitigation

Production 9ffe5b1 reports startup/OperationalError/53100 at gamma.py:238.
53100 can include Docker shared-memory segment exhaustion as well as disk full;
there was no basis yet for deleting evidence or buying persistent capacity.

New immutable OPERATIONAL profile database_v1.yaml sets
max_parallel_workers_per_gather=0 for app PostgreSQL sessions. Existing URL and
PGOPTIONS settings are retained, with this limit appended last. SQLite receives
no PostgreSQL option. GammaRepo now uses the same factory as the board, with
pre-ping and bounded session memory; own metadata and existing rows are kept.
All gamma calculations, signal/profile thresholds, PAPER rules and source data
remain unchanged. Large scans may have lower throughput in return for bounded
parallel-query memory. No server-wide setting or paid resource changed.

Failure.resource is a fixed enum derived only from explicit original driver
messages or known codes. Shared-memory classification precedes "no space left"
so the former is never incorrectly labelled persistent disk. SQL and parameters
cannot forge a driver reason. Unknown 53100 remains unknown capacity. No private
path, segment, table, SQL, URL or credential is returned.

No DELETE, DROP, TRUNCATE, transaction replay, cache cleanup, profile retuning or
API increase. The concrete `docs/postgres-capacity-runbook.md` explains preserving
evidence, verifying volume vs memory, the prepared software mode, actual runtime
recovery conditions and the external Railway access/capacity action if disk
remains full. This cloud workspace has no Railway/production DB credentials.

## Verification and delivery

- **18 new cases**: ten explicit/unknown capacity reasons with secret paths;
  SQL/params cannot spoof a reason; four PostgreSQL URL/environment option
  combinations; SQLite/ambiguous option handling; common GammaRepo factory
  preserving existing rows; fixed operational profile validation.
- Full hermetic gate: **5067 passed, 52 key-gated skips**; strict mypy **263 source
  files**; ruff, lock and diff clean. Existing assertions preserved.
- All three original frozen calibration/tournament/stress report hashes still
  validate. No research/strategy/forward_v2/profile change, no markup or new
  product implementation detail. Previous actual-report browser checks apply.
- Commit Phase 5.43.1, green PR/main CI, matching Railway SHA plus actual scan/
  desk/research status required. Report any remaining physical-capacity blocker
  honestly; HTTP liveness is not permission to claim measurement has resumed.

Next: actual resource reason and recovery, or owner-controlled Railway capacity
action described in the prepared runbook, preserving all recorded evidence.
