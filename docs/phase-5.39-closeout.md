# Phase 5.39 closeout — prospective data readiness

KAPALI — distinguish monitor liveness from usable data and an eligible hypothesis.
Owner authorizes continued work/deployment in chat. No new vendor request, data
write, quota change, hypothesis selection or PAPER publication/pricing change.

## Behaviour

Read-only diagnostics use actual as-of captured daily bars, the existing immutable
tournament hypothesis and the declared exchange calendar. Current valid bars,
continuous history, absent features, failed pattern/threshold/volume/market gates
and eligibility have separate, mutually exclusive counts over the fixed non-SPY
universe. They reconcile to original masks for ALL 7,776 configurations in a
synthetic causal fixture, without using that fixture as research evidence.

Expected completed session respects holidays, early closes and exact ET/DST.
Unknown years stay unknown; future observations, unsettled/invalid bar dates and
other symbols are excluded. Status distinguishes missing/stale data, waiting for
the prospective start, warm-up, partial coverage, no qualifying setup, missed
deadline and an already frozen cohort. Recomputed conditions never replace its
membership. Existing pre-open instructions are counted as observed, per arm and
config/report. Actual frozen cohorts stay recorded after the deadline.

Private /stratejiler adds one short status line and a collapsed explanation with
reason counts, actual first-list/API-arm counts and times converted to Istanbul.
All count calculations explicitly refer to the latest valid SPY session; if it
is stale, they cannot be interpreted as current market-wide absence of signals.
Daily-history readiness does not establish volume readiness or opening liquidity.

The separate timed, off-loop, single-flight job runs after the signal/research
attempt even when that attempt fails. Only safe aggregate dates/counts/status
enter /health, and only once the main job has actually cycled. No tickers, API
payloads, exception text or private evidence enter that summary. A timeout clears
old success; its read-only worker cannot later overwrite the failed check with
late success. No tables are created by diagnostics.

## Production observation

While this phase was under test, the preceding c08000f release reached Railway
SUCCESS and matching /health SHA, but its FIRST live-alpha cycle reported FAILED
with no desk/research statuses. The existing health endpoint did not reveal the
exception class. Do not claim that deployment as a healthy signal pipeline.
This phase ensures data readiness is observable independently. Separate loop
failure classification/recovery belongs to Phase 5.40, not an invented diagnosis
or a frozen strategy change. Original c08000f main CI passed.

## Verification and delivery

- **22 new cases** including nine actual calendar boundaries, causal/future bar
  exclusion, no-write empty DB, missing middle/SPY session and warm-up, old/start
  data, partial coverage vs no setup, full-grid reason/mask agreement, cohort/
  arm first availability/config separation, failure/cadence, late timeout thread,
  health privacy/lifecycle, actual private route and a failed live-cycle hook.
- Full hermetic gate: **4996 passed, 52 key-gated skips**; strict mypy **261 source
  files**; ruff, lock and diff clean. Existing tests/assertions preserved.
- Local Chromium actual frozen reports and labelled dummy DB at 390/1440px:
  readiness details collapsed by default, all tables expanded without overflow,
  zero WCAG A/AA axe issues or JavaScript errors. No production-authenticated
  inspection claimed. Existing cached styles reused unchanged.
- Old calibration/tournament/stress package/profile/report hashes and the v2
  forward implementation remain byte-identical. No scoring-module truth table
  applies. Green PR/main CI and final release/job verification follow delivery.

Commit: Phase 5.39.1. Next: make failed scan recovery and safe cause observable;
never confuse a matching deployment SHA with a healthy signal/data pipeline.
