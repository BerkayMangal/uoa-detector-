# Phase5.67.2 — native advisory-lock connection headroom

Registered2026-11-10 before code. Mainb02a82f is green/deployed, source healthy,
but guardian reports storage_unavailable before any claim/provider request.
The advisory lock holds one PostgreSQL connection. The unchanged verified
coverage reader holds a Session connection while inspecting DailyReceipt through
a separate Engine connection. A two-connection pool cannot serve all three;
bounded pool exhaustion is the concrete suspected cause, to reproduce locally.

Allow exactly three pooled connections/maxoverflow0 for the new guardian/manual
wrapper engine, preserving the statement/lock/pool timeouts and all data/API/
window/claim/capture/strategy limits. The third connection is read inspection
headroom, not another worker, request budget or database instance. Do not modify
original readers/verifiers to evade checks or change original source identities.

Reproduce real reader behavior with SQLite QueuePool and an occupied advisory-
lock equivalent: pool2 fails closed before network, pool3 completes the same
verified coverage observation. Check bounded configured pool settings and shared
admission. Full unchanged gate, exact-green feature/main CI and native healthy
guardian/current coverage/old-record parity precede completion. No previous
billing, captured source or old decision is replayed; no native claim exists for
this expected session before this fix.
