# Phase 5.42 — signal-thread startup visibility

Frozen before implementation, 2026-10-28 Istanbul. Matching live 0c25b73 responds
to /health but has no live_alpha or research_data summary after startup. Existing
_live_alpha_thread_main logs pre-loop crashes and restarts after 30s, but publishes
no status before a first completed loop. This hides initialization/connection
failures, including ones before Phase 5.40's guarded cycle. Do not invent the
underlying cause from missing status alone.

At each actual supervisor start publish status=starting, current aware timestamp,
null mode/cards and no old failure/counts. If the coroutine exits or raises before
a cycle completes, publish status=failed with the existing safe Failure structure
and stage=startup; extend that stage's fixed allow-list. Unknown normal exit has
a fixed runtime failure, not arbitrary text. Preserve the existing 30s restart
interval and no additional broker/vendor/transaction retry.

Use the same SQLSTATE/disconnect allow-lists and relative project-code locations.
No exception message, stack text, SQL, parameters, absolute paths, API data or
credentials enter public health. A later actual live cycle replaces starting/
failed status normally. Disabled worker/no running job keeps old empty health
behaviour. Do not modify other supervisors or frozen scoring/research/data.

Test actual supervisor initial state, safe failure with storage SQLSTATE, normal
unexpected exit, restart state clearing and actual successful loop overwrite;
no real waiting or API. Full hermetic pytest, strict mypy src/webapp, ruff/lock/
diff before commit; green PR/main CI; matching Railway SHA and actual signal/data
status before claiming recovery. Continue on the concrete resulting diagnosis.
