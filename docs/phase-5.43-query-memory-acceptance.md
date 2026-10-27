# Phase 5.43 — resource-specific diagnosis and bounded PostgreSQL query memory

Frozen before implementation, 2026-10-28 Istanbul. Production 9ffe5b1 explicitly
reports startup/OperationalError/SQLSTATE 53100 at gamma.py:238 (catalog/schema
inspection). Earlier failures occurred at snapshot commit and the source-session
read; independent data reads previously succeeded. 53100 is PostgreSQL disk_full,
but can also mean Docker dynamic shared-memory segment exhaustion. Do NOT assert
that the persistent volume is full or purchase storage without differentiating it.

Expose a fixed resource enum in the EXISTING sanitized Failure: shared_memory,
temporary_storage, disk, connections, memory, or unknown/absent. Recognize only
explicit known server phrases/codes. No raw server message, segment/file/DB name,
SQL, URL, credential or exception argument is public. Keep original category,
SQLSTATE and invalidated flag. SQLSTATE 53100 by itself remains capacity-unknown.

Configure application PostgreSQL connections to max_parallel_workers_per_gather=0
under a new separate immutable operational profile database_v1.yaml. This is
session-local, limits demand for dynamic shared-memory segments and preserves
query results; it may reduce throughput on large scans. Do not alter server-wide
configuration, work_mem, schema, quotas, scores, hypotheses or actual data.
Preserve existing URL/PGOPTIONS connection options when appending this setting.
SQLite receives no PostgreSQL connect argument. Keep pre-ping. Route GammaRepo's
engine creation through the same tested board factory; preserve its URL mapping,
own metadata and existing rows. No other gamma scoring/calculation change.

This can address shared-memory capacity; it DOES NOT free persistent disk, repair
a volume, replay a failed transaction, increase a paid service or erase evidence.
No DROP/DELETE/TRUNCATE/VACUUM FULL/retention/reset, external purchase, broker order
or added vendor call. Frozen historical sources/profiles/reports/forward_v2 stay
unchanged. If actual disk/temp storage remains full, prepare a concrete Railway
capacity runbook and report the external access/capacity blocker truthfully.

Test resource-message sanitization with secret-bearing paths/identifiers; unknown
capacity remains unknown; exact PostgreSQL options with existing URL/environment
options; SQLite unaffected and retains evidence; GammaRepo uses bounded factory
without dropping/resetting rows; existing score/gamma tests unchanged. Full
hermetic pytest, strict mypy src/webapp, ruff/lock/diff; green PR/main CI; matching
Railway SHA plus actual cycle/desk/research status. Do not claim recovery from
HTTP 200 or a green build alone. Capacity mode/proof and operator next step must
be concrete and reviewable before any required external-access request.
