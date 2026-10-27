# Phase 5.42 closeout — signal-thread startup visibility

KAPALI — supervisor state now exists before the first completed engine cycle.
No claim that the underlying production storage/connectivity problem is repaired.

Actual 0c25b73 reached matching live SHA/HTTP 200 but had no live_alpha or
research_data summary while being observed after startup. Previously pre-loop
failures only reached logs, with a 30s restart; a silent pending initialization
was also indistinguishable from a disabled job in public health.

Each actual supervisor attempt now publishes starting + aware timestamp and
clears previous failed mode/card state. A startup exception uses the existing
safe SQLSTATE/disconnect/category/class/relative-source hint with fixed startup
stage. Unexpected coroutine return also records a fixed failed-runtime status.
The existing 30s restart, worker opt-in, actual cycle state writes, quotas,
transactions, profiles, research results and data all remain unchanged.

Verification: **3 new actual-supervisor cases** cover initial visibility with
secret-bearing SQL failure, unexpected normal return, repeated starting-state
cleanup and overwrite by a successful running coroutine. No real sleep or API.
Full hermetic gate: **5049 passed, 52 key-gated skips**; strict mypy **262 source
files**, ruff/lock/diff clean. Original assertions remain intact. No markup or
frozen artifact/profile/runtime-v2 change; existing browser checks apply.

Commit Phase 5.42.1; green PR/main CI, matching Railway SHA and actual startup/
cycle evidence required. Next: fix the concrete safe startup diagnosis or confirm
actual successful signal/data cycles. Liveness alone is insufficient.
