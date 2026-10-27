# Phase 5.41 — database write failure diagnostics and connection hygiene

Frozen before implementation, 2026-10-28 Istanbul. Actual production a3f5c40
reports scan/storage/OperationalError at webapp/live_alpha/store.py:204, the
scan snapshot's transaction commit boundary. Independent readiness SUCCEEDS:
expected/latest 2026-10-27, current 12/70 names, history-ready 11, eligible 1,
waiting for prospective start. Do not misdiagnose that as missing source bars.
The driver cause remains unknown from the existing safe hint.

Owner authorizes continued reliability work/deployment. Extend the safe failure
hint with an allow-listed standard SQLSTATE and SQLAlchemy connection-invalidated
boolean. Recognize known states for disconnect, resource capacity, timeout/lock,
constraint, readonly and missing relation; unknown codes stay absent. No raw
driver/exception text, SQL, params, server identifiers or credentials are public.
Missing sqlstate/pgcode must work for SQLite and driver-neutral failures.

The shared Alfa Board engine factory currently reuses pooled connections without
a health check. Enable SQLAlchemy pool_pre_ping before checkout. This prevents
known-dead pooled connections being handed to a new transaction; it does NOT
replay an active failed transaction or repair disk/capacity/schema errors. Keep
transaction rollback/append-only identities and all data untouched. Do not claim
the observed write failure is repaired until the actual production cycle proves it.

No new API call, quota-cap/profile/scoring/hypothesis change, schema alteration,
data deletion, paid resource or broker order. Archived research/profile/artifact
and forward-v2 files remain identical. No automatic retry of write_scan: a commit
whose reply was lost has an uncertain outcome and cannot be blindly replayed.

Tests: known/unknown/malformed driver codes and disconnect flags, SQL-bearing
secret sanitization, category mapping, normal SQLite writes after checkout,
deliberately killed pooled connection replaced before a new transaction, factory
URL behaviour retained. Full hermetic pytest, strict mypy src/webapp, ruff, lock
and diff before commit; green PR/main CI; matching Railway SHA plus actual
scan/desk/research status. Continue on any concrete remaining diagnosis using a
new scope rather than declaring liveness to be pipeline recovery.
