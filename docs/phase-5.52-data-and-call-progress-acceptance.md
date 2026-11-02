# Phase 5.52 — current data readiness and readable call progress

Frozen 2026-11-02 before implementation. Owner accepts the proposed coverage,
measurement and usability work, with existing test/push/merge/deploy authority.
No broker, execution, paid plan, live threshold or historical verdict change.

## Current-data repair

The morning audit found 35/82 research names ready, 47 missing/stale; the eleven
sector ETFs are absent. This is research/context coverage, not a claim that the
live 34-name universe is broken. Add a bounded operator command, preview by
default, to fetch the existing UW ohlc/1d endpoint only for unready frozen members.
No web request or recurring polling loop starts a repair. Maximum 82 requests,
>=5000 native daily quota reserve, >=.75 seconds spacing, timeout30 seconds;
stop without automatic retry on auth/quota/transport/HTTP errors. Use bound
credentials, never persist or print them or a database URL.

Keep at most the last 60 completed exchange sessions, enough for the existing
50-session price warmup. This is operational history, not the 80-session causal
flow-calibration requirement or a new backtest. Ignore incomplete/current and
nonregular-session bars, validate OHLC/date/duplicate identity, preserve missing
and conflicting data as unavailable. No Study G file or labels are used for
selection, retuning or performance estimation.

Insert only missing immutable daily-bar keys in the existing calibration table;
never overwrite old prices, decisions, cohorts, outcomes or daily-job markers.
Stamp new bars after the actual response, and adopt physical post-commit hash
receipts through the existing conservative availability helper. Existing first
receipts stay fixed. Raw packets/quota/source evidence remain private outside
git. Preserve before/after coverage, exact request counts, inserted/conflicting
keys, error categories and receipt times in a nonoverwriting run manifest.
A partial run cannot claim full coverage. Existing scheduled close jobs remain
unchanged. The operator is allowed to perform this append-only repair once its
software checks pass. Frozen pre-repair research rosters stay frozen.

## Call progress

Make the private call list show +15/+60-minute underlying-reference returns,
matched SPY difference and explicit time/data status without opening each card.
Use the existing opening profile's horizons and target/calendar semantics, not
new performance assumptions. Count only original first-AL records; updates link
to their original and never inherit its return. Summary counts describe only
the displayed/filter scope, not a global strategy sample or alpha estimate.

Distinguish not-yet-due, due-awaiting, missing-price, missing-benchmark, invalid,
unknown-calendar and observed states. Invalid/future/mismatched targets, source
or profile records cannot acquire a measured result. Keep horizon/policy cohorts
separate. Do not turn missing into zero, assume fees, infer fills, or pool PAPER
option P&L with underlying references. The detailed scorecard retains all original
reasons, plan, costs and provenance, with clear results first and folded details.
Mobile users can read the main figures without sideways scrolling. No win-rate
or new calibrated probability is invented from the four current records.

Keep existing routes/auth/private-no-store/noindex boundaries. Page rendering
runs SELECT-only bounded queries, no vendor API, optimizer or write. No change
to the frozen scorecard/storage/forward/capture implementation or research hashes
is necessary; new read-only adapters may reuse them.

## Delivery

Meaningful tests cover real failures: causal receipt timing, final sessions,
first-write/idempotency/conflicts, malformed/gapped sources, quota/auth stops,
first-entry ownership, exact targets, time maturity, partial/invalid outcomes,
bounded batch reads and no writes/vendor calls on GET. Preserve all old tests.
Full hermetic pytest/mypy/ruff/lock gate, green PR/main CI, actual Chromium desktop
and mobile checks, measured production coverage and exact deployed SHA required.
Report genuine remaining gaps or missing measurement honestly; no alpha claim.
