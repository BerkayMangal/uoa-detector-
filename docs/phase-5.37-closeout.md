# Phase 5.37 closeout — frozen first daily PAPER roster

KAPALI — bounded first family/session publication under partial data and concurrent
publishers. Forward-only correction; no historical parameter, score or report edit.
Owner authorizes continued tool/research improvements and deployments in chat.

## Reproduction and correction

A local synthetic reproduction of the original forward implementation configured
one daily name. First NVDA bars yielded one PAPER unit; later, better-ranked MSFT
bars yielded a second unit that day. Applying top-N on EVERY observer pass was not
a fixed daily roster. This was detected before prospective signal dates begin on
2026-10-28 ET; no real orders or production-position changes are involved.

New append-only `alfa_tournament_cohort`: one unique key per report/config/session,
containing the first ranked eligible members, inputs and API contexts. Database
first-writer wins. Partial later data cannot replace/add names or arm membership.
No eligible names means no cohort yet; once any roster is frozen, it may contain
fewer than the maximum if data coverage was incomplete. This is explicit available-
data selection, not a retroactively reconstructed complete-universe ranking.

Publication availability is checked again after context collection and before
individual instruction writes. A slow observer that crosses the next opening
cannot backdate a new instruction using its earlier input-snapshot timestamp.
Availability is at least both snapshot time and physical publication-clock time.
Future cohorts are unavailable to an earlier observer. Existing positions continue
to price their exact session endpoints even after the day's roster is frozen.

Pre-existing observations/decisions are not deleted. Existing within-cap members
freeze the cohort; an already-over-cap legacy cohort blocks ANY new publication
while old evidence remains. No quota/vendor/broker request or actual sale added.

## Frozen research, versioned runtime

The ORIGINAL tournament package and all three original artifacts remain byte-for-
byte identical to feature commit `6adf63b`; both the tournament and previous
calibration report hashes still validate. Historical strategy functions, selection,
null draws and parameters are unchanged; no optimization rerun.

Its artifact hashes the entire original package including the old monitor. The
operational correction therefore lives separately in `webapp/tournament_forward.py`,
version `tournament_forward_v2`, using the same validated feature/context/settings
functions and existing PAPER tables/identities. Exact-session pricing rules remain
the same. New cohorts/records carry this runtime version and source hash. The
actual live-loop runner imports v2 and shares its status with the existing private
view/safe public health summary. The archived implementation is retained for
research reproduction, not used for new production publications.

## Verification

- Six new behavioural cases: partial-arrival membership/arm freeze plus continued
  entry/exit pricing and dedup; simultaneous competing publishers reading one
  winner; future/late observer exclusion; preserving legacy over-cap evidence;
  isolated runtime failure; slow-context publication crossing the next open.
- Focused archived/new/runtime/durability suite: 34 pass.
- Full hermetic gate: **4958 passed, 52 key-gated skips**; strict mypy **256 source
  files**, ruff/lock/diff clean. Only additive table classification changes an
  existing test. Original tests/assertions remain intact.
- Existing desktop/mobile browser report checks remain applicable: this change
  retains private routes, markup, styles and existing record schemas.
- Owner authorizes green PR/main CI merge and Railway deploy. Verify matching SHA
  and actual v2 job status before reporting completion; authenticated production
  browsing remains unavailable here, with local browser checks already recorded.

No scoring truth tables apply. Next evidence is prospective coverage/outcomes,
not declaring the reused-data tournament to have found validated alpha.
