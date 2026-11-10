# Phase5.67.1 — historical read navigation and per-wire maintenance window

Registered2026-11-10 before either change. Engineering clarification of5.67,
not a change selected from strategy outcomes.

## Shared maintenance exclusion and per-wire guard

Guardian and manual startup entry must honor the same cross-process PostgreSQL
session advisory lock. The lock is held across the original one-shot operation,
released in finally; unknown unlock invalidates/closes the physical connection
instead of returning a locked connection to a pool. Preexisting pending/orphaned
manual attempts remain a hold. A partial/failed same-settled-session manual result
cannot automatically become another guardian attempt. Explicit new manual
requests retain their one-shot semantics. SQLite test exclusion must not be
mistaken for production distributed locking.

The05:00–09:15ET window applies immediately before EVERY actual outbound request,
not merely initial admission. Introduce a backwards-compatible optional request
hook through existing repair_operator.once/daily_repair.run and an httpx request
event hook. DefaultNone preserves existing manual behavior. Guardian's hook checks
actual current clock/window/expected settled day after internal spacing and before
send; stop safely with a stored result, no provider call after cutoff and no retry.
Do not edit frozen history_capture transport/source files or their identities.

## Read-only date navigation

The new engine's four private APIs already accept a selected date, but the
existing research HTML always shows today. Add an optional validated day query
to /arastirma for the forward engine/source/news/fundamental/diagnostic projections.
Other historical-study/pilot summaries keep their existing populations. Clearly
label the selected measurement date. Provide ordinary previous-session/today links
only, preserving fifteen main navigation links, one alert controller, and no new
script/form/nav element. Never link before this cohort's activation or into future
sessions. Bound historical reads exactly as existing views do. GET remains
SELECT-only, with no provider call, fitting, DDL or record rewrite.

A small expandable source-quality explanation may display the bound diagnostic
code's plain-language meaning per candidate. No headline/body/secret payload is
rendered by this diagnostic. Past UNKNOWN decisions remain UNKNOWN; additional
metadata is explicitly retrospective inspection of the original immutable source,
not information asserted available to the original decision.

Verify backward-compatible hook defaults, per-wire boundary/spacing stop and
cross-path exclusion; authenticated date selection/historical read parity,
source clocks and no future navigation. The same full gate and exact native
release/Nov9 record parity requirements apply as5.67.
