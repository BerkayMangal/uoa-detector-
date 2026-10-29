# Phase 5.47 — deterministic deployment and outage isolation

Frozen before code, 2026-10-29 Istanbul. Owner explicitly requests immediate
repair and deployment of ALL developments; no further confirmation needed for
code/push/merge/deploy. Existing source main 5d74a1b includes phases 5.45/5.46;
production still serves cdc7add. New owner log confirms native recovery mode,
SSL unexpected EOF, and Railway 500 logs/sec with dropped messages. The pasted
Railway agent says build failed before install/build; this is not itself a native
builder log or proof of a platform cause. No such cause will be invented.

Local read-only controlled proof: GammaRepo initialization inside the async gamma
loop blocks a web-loop callback scheduled at 20ms until ~266ms when its simulated
250ms synchronous DB wait ends; zero real database/vendor calls. The live worker
and board refresher also synchronously initialize their database on that loop.

## Deterministic build and runtime verification

Add an explicit Dockerfile and Railway Docker builder selection. Use a verified
Python 3.12 slim image and pinned uv 0.11.12 (CI version); install uv.lock main
runtime dependencies only and a non-editable project wheel. Preserve required
runtime source/profiles/artifacts so old frozen report hash validation still
works. Copy no secrets, .env, .git, host virtualenvs/caches, local SQLite stores,
logs or uploaded attachments. Add .dockerignore. Preserve current deployment
start command, healthcheck path/timeout and restart policy. PATH exposes venv
uvicorn. Respect runtime PORT; no hardcoded production address/port or secret.

Actually build and start the final Docker image, with lifespan/profile loading,
health SHA, auth/public/private surfaces and CSS. Test a controlled local blocked
background failure without any external API credentials/calls or production DB.
No claim that local Docker success repairs the actual PostgreSQL service or
proves why the earlier Railway builder failed. No repeated speculative retries.

## Protect the web loop during PostgreSQL failure

Run complete ingestion, gamma and board-refresh coroutines on dedicated daemon
threads/event loops for PostgreSQL deployments, preserving each client's/session's
single loop/thread ownership. The existing _supervise policy still owns restart
and its 30s delay. No overlapping restart while a prior operation is running.
Propagate normal return/errors and cancellation; cancel the owned loop task,
close async generators/remaining tasks and loop after its existing cleanup.
Cancellation before thread start must not start work. Blocking native driver I/O
cannot be forcibly canceled, but must not block HTTP/shutdown or cause an extra
retry. Do not use the shared web thread pool for infinite background services.
SQLite paths keep their existing same-loop behavior (in-memory/thread affinity).
Existing live-alpha thread and already offloaded prerender path stay compatible.

Keep the direct synchronous health() Python helper/schema for existing callers,
but serve GET /health via a thin async memory-only wrapper so synchronous request
pool exhaustion cannot starve liveness. HTTP200 continues to mean liveness only;
actual cycle/database/readiness states remain honest and unchanged.

## Bounded expected failure logging

Condense SQLAlchemy storage exceptions on the known background supervision,
live-alpha startup/cycle/heartbeat and gamma/worker retry paths to ONE safe log
record without a traceback/native message/SQL/params/connection URL. Preserve
component, safe class/category/source-code site/SQLSTATE/resource and allowlisted
native connection reason. Non-storage programming/vendor exceptions retain their
existing stack traces and retry semantics. No new retries, cap/provider calls,
transaction replay or swallowed/counted-as-success storage operation. Retain
existing durable error evidence/statuses; do not rewrite records to hide failures.

Recovery/EOF does not justify disabling TLS, changing database target, resetting
WAL, promoting/recreating/replacing the database or deleting evidence. No paid
resource changes. Owner declines another request for the same log; continue with
provided evidence without repeatedly asking. Native PostgreSQL recovery remains
an external condition unless actual access/evidence permits a concrete repair.

## Verification and delivery

Dedicated tests for loop/thread/client ownership, context propagation, blocking
startup vs responsive HTTP, return/error and cancellation including start races,
cleanup, no overlap/retry changes, SQLite compatibility and thread-pool-saturated
health. Storage log compactness/secret isolation and unexpected exception trace
preservation, including native recovery plus SSL EOF. All existing assertions and
frozen policies/profiles/research/verdicts unchanged. No numerical trading rules,
new vendor requests/probes, fake trades, fabricated samples or retrospective calls.

Full hermetic pytest, strict mypy src/webapp, ruff/lock/diff before each commit;
actual Docker build/run proof and private UI checks. Green PR/main CI and matching
Railway runtime SHA required. Distinguish publishing all features from real
signal/PAPER/opening capture; report unrecovered native database state plainly.
