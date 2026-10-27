# Phase 5.41 closeout — database connection hygiene and safe driver evidence

KAPALI — pre-ping idle pooled connections before a new checkout, and distinguish
safe database failure causes with known SQLSTATE/disconnect metadata. This does
not by itself prove the production a3f5c40 snapshot-write failure is repaired.

## Evidence and change

Actual production first cycle failed at scan snapshot commit, OperationalError,
webapp/live_alpha/store.py:204. The independent data check succeeded with current
SPY/session 2026-10-27, valid 12/70 names, 11 complete histories, one currently
eligible experimental hypothesis and waiting-start status. No source-data outage
or hypothesis verdict is inferred from that storage error.

The shared board engine now uses pool_pre_ping. This checks an idle connection
before handing it to a new transaction; failed ACTIVE transactions are NOT replayed.
Snapshot/recommendation/PAPER rows, keys and append-only history remain untouched.
No write_scan retry, data reset, schema change, paid upgrade or new API request.

Safe failure hints add allow-listed standard SQLSTATE and a boolean connection-
invalidated flag. Known codes classify connection failure, capacity, timeout,
lock/conflict, readonly, missing relation, authorization and I/O. Unknown/malformed
codes are omitted; driver metadata getter errors cannot replace the original
failure. SQLite missing-code messages map only to fixed resource/readonly/I/O
categories; their text, SQL, parameters and credentials are never public.

## Verification and delivery

- **27 new cases**: 15 known driver-code categories, six unknown/malformed codes,
  old-driver pgcode/disconnect fallback, three SQLite no-SQLSTATE categories,
  malformed driver metadata, and a real SQLite file-backed killed-idle-connection
  recovery that retains both old evidence and a subsequent new write.
- Full hermetic gate: **5046 passed, 52 key-gated skips**; strict mypy **262 source
  files**; ruff, lock and diff clean. All existing assertions preserved.
- Original research/calibration/stress hashes, profiles and forward_v2 source
  unchanged. No markup change; previously verified actual-report mobile/desktop
  overflow/accessibility/JS checks remain applicable.
- Commit Phase 5.41.1; green PR/main CI, matching Railway SHA and actual subsequent
  scan/desk/research status required. Continue with the driver evidence if writes
  remain unavailable; do not equate liveness or successful reads with durable writes.

Next: actual production recovery, or a separate scoped fix for its concrete code.
