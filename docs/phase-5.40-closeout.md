# Phase 5.40 closeout — failed-cycle diagnosis and recovery cadence

KAPALI — failed cycles expose a sanitized stage/category/project source location
and retry using the existing live cadence outside market hours. This does not
claim the c08000f underlying exception has already been repaired; actual cause
and recovery must be observed after deployment.

## Behaviour and decisions

Track scan/shadow/desk/calibration/tournament stage before its actual attempt.
Public failed-cycle hint contains a fixed category, allow-listed exception name
and last project-owned relative source file/line only. No exception message,
SQL, arguments, payload, host path or credentials cross this boundary. Private
legacy logging/heartbeat detail is retained. Later successful/degraded cycles
clear old failed hints. Phase 5.39 readiness remains independently observable.

Failures outside live/premarket hours now wait min(existing live, existing closed
cadence): **300s** rather than 1800s under the frozen profile. Healthy/degraded
closed cycles keep 1800s. No policy/profile number, new endpoint, quota cap or
PAPER transaction changed. More prompt failed-scan retries remain subject to
existing atomic daily reservations and existing append-only/idempotent records.

No original calibration, tournament, stress implementation/profile/artifact or
forward_v2 source changed. This phase diagnoses an observed failed first cycle;
it does not retune any strategy to recover a desired backtest result.

## Verification and delivery

- **23 new cases**: 13 known-error classifications with secret-bearing messages/
  SQL; owned vs external source paths and generic unknown class/stage; all four
  calendar modes' original/recovery cadence; five exact failed stages through
  the actual loop, followed by recovery, hint cleanup, retry delay, readiness
  continuation and zero vendor calls.
- Full hermetic gate: **5019 passed, 52 key-gated skips**; strict mypy **262 source
  files**; ruff, lock and diff clean. The preceding Phase 5.39 commit independently
  passed 4996/52 and mypy261 before this work.
- No new markup; Phase 5.39's actual-report 390/1440px browser, overflow, axe A/AA
  and JavaScript checks remain applicable. No protected production view claimed.
- Both independently green phases delivered in one PR, followed by green main CI
  and matching Railway SHA. Require actual scan/desk/research statuses for recovery;
  Railway SUCCESS or /health HTTP 200 alone is insufficient.

Commit: Phase 5.40.1. Next: read the safe failure evidence and fix any concrete
remaining defect in a new frozen scope, or record actual successful recovery.
