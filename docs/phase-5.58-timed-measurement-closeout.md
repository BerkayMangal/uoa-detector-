# Phase 5.58 — prospective timed measurement and daily report

The old opening observer had qualified marks for zero of fifteen +15-minute
targets and one +60-minute target, despite persisted later observations. This
release adds an independent collector for new first publications and a private
daily report at `/olcum` (`/api/olcum`). It never rewrites old missing marks or
changes any strategy threshold, fixed experiment, source profile or news vote.

The new stream binds to a durable activation and exact first BUY/source identity,
then captures bounded UW stock/SPY last-trade references at publication+15/+60
minutes. Original entry references stay fixed; missing original SPY cannot be
repaired from a later update. Same-attempt pairs are selected in attempt order,
never by return. Actual post-commit physical clocks control admission. Ineligible,
late, missing, partial, rejected, unknown, pending and complete remain distinct.

Nine append-only tables retain activation, targets, ownership, conservative
request slots, bounded raw packets, post-commit receipts, completions, assessments
and day guards. Single-attempt transport preserves native proxy/CA behavior and
default behavior for existing clients. Atomic module/shared reservations retain
fresh native usage floors on refusal. Auth/quota guards and temporary cooldowns
precede packet processing. A restarted worker holds requests behind unresolved
pre-worker claims; a later visible old claim is rechecked before each grant.
Expired unresolved provider outcomes halt the day as unknown, never inferred auth.

The read-only report separates each new minute horizon from old gross close
marks and model-cost PAPER closures grouped by policy/instrument. Zero targets
have no success rate or return. Truncated or unverifiable cohorts are explicit;
corrupted evidence cannot preserve a numeric return. Stored Mahalle/Stajyer
connection controls and unchanged UW five-arm readiness are shown separately.
No learned probability, automatic training, causal news contribution, portfolio
equity or profitable strategy is claimed. All fourteen old navigation links and
the global PC notification controller remain, with a fifteenth measurement link.

## Engineering decisions

1. No OpeningObservation bridge: that table has no purpose/profile ownership and
   added SPY records could change future legacy entry references. A new method
   therefore has its own data and clearly labelled panels.
2. Source identity uses the exact validated ticker endpoint. Both documented and
   real quote responses omit ticker echoes; any supplied ticker/symbol alias
   must match. This does not invent a required vendor field or use a fallback.
3. There were two managed-session key probes, then an in-memory comparison proved
   that key different from production. Phase5.58.1 was registered before exactly
   two authoritative production-key probes. The new bounded transport succeeded
   for AAPL/SPY at11:04:30UTC, HTTP200,844/848B, native counts168/169. Both were
   premarket and are capability evidence only, never outcome observations.
4. Physical request, receipt, post-packet confirmation and final post-write
   availability remain distinct. Completion records retain the actual returned
   per-leg confirmation instants so restart projections cannot use earlier
   pre-commit samples. Sequential quotes are near-time references, not fills.
5. A missing current-day usage floor refuses requests rather than inventing zero.
   Unreadable429 responses halt conservatively as quota_unknown, distinct from
   proven daily exhaustion. Reservations are upper bounds, not proven wire counts.
6. The module is opt-in via TIMED_MEASUREMENT_ENABLED. PostgreSQL work uses the
   existing isolated native worker lifetime and bounded statement/lock timeouts.
   GET never initializes tables, contacts vendors, trains or writes outcomes.

## Verification and release evidence

Meaningful tests exercise real publication/discovery/transport/storage/projection,
both directional return signs, missing original SPY despite later updates,
concurrent claims/caps, restart and rolling-replica visibility, partial/fatal/error
paths, real timeout persistence, post-commit lateness, corrupt evidence and cohort
metadata, calendar edges, read-only/private GETs and ASGI liveness during blocked
native I/O. Frozen opening/news source/profile bytes are independently pinned.

Full hermetic gate:6,088 passed,52 key-gated skips,two existing warnings,164.10s.
Strict mypy passes over334 source files; Ruff, lock and whitespace checks pass.
Exact native release SHA/deployment/activation/render proof is recorded in the
pull request and private operational artifacts. Local UI
proof covers real typed DTO fixtures at1440/1024/390/320 with fifteen visible
links, open detail sections, one global alert controller and no page overflow.
This is not proof of delivery to the owner's OS notification center.

Until a real post-activation call reaches its target, capture is explicitly
awaiting its first live observation. Capability probes and fixtures cannot
demonstrate prospective coverage or alpha. Versioned calibration remains future
work after honest data collection and evaluation on new observations.
