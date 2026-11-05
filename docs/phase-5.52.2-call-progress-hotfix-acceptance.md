# Phase 5.52.2 — canonical call outcome labels

Registered before implementation, 2026-11-05. The owner's existing measurement,
bug-fix and ordinary publication authorization applies within Phase 5.52's
read-only call-progress adapter. The morning measurement audit found 15 first
calls across five sessions; `/acilis` correctly shows one recorded +60-minute
result while `/karne` and individual cards incorrectly show every result awaiting.

The persisted writer uses horizon IDs `m15`, `m60`, `c0`, `c1`, `c3`, `c5`;
the measurement reader consumes display labels. Normalize these IDs through the
current frozen profile in `webapp/call_research/progress.py` before aggregation
for both list and detail. Preserve existing display-label compatibility and
invalidate duplicate or canonical/legacy-alias collisions without last-write
selection. Unknown IDs cannot acquire results. Keep future/invalid/source/profile,
first-entry/update and benchmark guards, bounds, existing tests and route privacy.

No database rewrite/backfill, new observation, vendor request, policy/profile,
opening writer/measurement source, historical outcome, fee assumption or strategy
change. Real missing intraday prices stay missing; display correction cannot
manufacture their returns. First-call gross references and legacy instrument PAPER
remain separate; no improved performance or alpha claim follows from this repair.

Regressions use actual `opening.outcomes.measure` output, not display-key fixtures,
and cover minute/daily labels, missing/partial/invalid/future outcomes, source and
update guards, alias collisions, list/detail/opening parity and read-only GETs.
Run the unchanged complete pytest/mypy/Ruff/lock gate before commit, exact green
PR/main CI and ordinary manual merge. Verify the native exact SHA, healthy scan,
first-call counts and matching list/detail status/results against the opening
summary with stored records only. Report residual measurement gaps explicitly.
