# Phase 5.39 — prospective research data readiness

Frozen before implementation, 2026-10-28 Europe/Istanbul. The owner authorizes
continued research/tool improvements and deployment. Status "ok" on a monitor
does not establish sufficient data or a qualifying signal. Make those conditions
observable without modifying any strategy, frozen artifact or publication rule.

## Read-only inputs

Use ONLY already captured daily research bars and original tournament report/
settings/calendar plus existing first-observation audits/cohorts/decisions.
No vendor requests, new paid services, quota-cap changes, data resets, backfills,
threshold changes, model promotion or broker orders. The archived tournament,
calibration and stress packages/reports/profiles remain byte-for-byte unchanged.
The versioned forward_v2 publication/pricing implementation remains unchanged.

Expected settled session uses the declared exchange calendar, including holidays,
early closes and exact ET/DST conversion. Unknown years are explicitly unknown.
Observation time is a supplied aware timestamp. Future observations, future/
unsettled bar dates and out-of-universe names must not enter coverage. Preserve
the distinction between a valid current bar and enough consecutive history.

For each already PAPER-enabled family, count all fixed non-SPY names into
mutually exclusive reasons in this priority: current bar absent/invalid;
incomplete contiguous valid price history/reference or global feature warm-up;
unavailable/nonfinite features; base pattern not met; numeric score threshold
not met; volume absent/threshold not met where required; market-regime filter;
eligible. Counts must reconcile to the full fixed non-SPY universe; missing names
are not failed signals. Use the original configuration/feature/mask functions.
Diagnostic price-history readiness does not imply volume readiness or liquidity.

Display latest valid SPY day versus expected settled day, valid current-bar and
history counts, next instruction deadline and whether still open. A deadline is
the exact next declared regular open, never a guessed future weekday. Preserve
first frozen roster membership and count only instructions actually observed by
the timestamp, with per-arm counts (missing/degraded API data never inferred as
confirmation). Recomputed eligible names are diagnostic and cannot rewrite an
existing cohort; explain that difference in the private UI.

## Runtime and product

Add a separate bounded, off-loop, single-flight readiness job after the existing
research observer in the live loop. Reuse the existing tournament monitoring
cadence/timeout. Failure remains isolated; status must not report stale successful
counts as the new observation. No persistence tables or writes. Publish only a
safe allow-listed aggregate summary in /health (counts/dates/status only, no
tickers, positions, raw API data, arbitrary exception text or private evidence).
Absent job state leaves the old /health response unchanged.

On private /stratejiler show a brief readiness line and collapsed detailed reason/
arm counts. Keep all new details collapsed to retain a concise page. No new public
data routes or navigation. Existing auth/noindex/private-no-store stay intact.
Before the prospective start, report waiting-start even if old valid bars exist.
If expected current bars are absent, report missing/stale coverage rather than
claiming zero signals. An empty/partial valid universe is never market-wide "no
setup". Display last check timestamp and unknown fields rather than invented data.

## Verification

Hermetic cases: exchange early-close/holiday/weekend/DST and unknown year;
future observations/unsettled/invalid/out-of-universe bars excluded; latest-vs-
expected missing coverage; warm-up and a missing middle session; no-setup versus
data absence; reason reconciliation and exact original-mask agreement; first
cohort/instruction counts as-of and deadline equality; job isolation and sanitized
health output; real private route rendering. Full pytest, strict mypy src/webapp,
ruff/lock before commit; local mobile/desktop overflow/axe/JS checks; green PR/main
CI and matching Railway SHA plus actual job status before claiming deployment.
Production-authenticated visual/data access is unavailable and must not be claimed.
