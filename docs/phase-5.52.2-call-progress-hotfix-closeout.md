# Phase 5.52.2 — canonical outcome visibility

The actual opening writer stores `m15/m60/c0/c1/c3/c5`; the progress adapter
previously passed these IDs to a display-label reader. Both list and detail
therefore showed mature persisted outcomes as awaiting. A shared profile-driven
normalizer now supplies display labels at the read-only metrics boundary.
Canonical and legacy aliases remain compatible; duplicates and alias collisions
are invalid rather than a last-result selection. Unknown IDs stay unmatched.

Seventeen new regressions use the actual outcome writer, including minute and
all daily horizons/DST, recorded missing/partial results, source/update/future
guards and alias collisions. The initial reproductions failed 11/14 before the
fix, then passed; expanded focused coverage passes 66 cases. Full hermetic gate:
5,890 passed, 52 key-gated skips, two existing warnings, 150.77 seconds. Strict
mypy over 325 files, Ruff, lock check and independent review pass.

Native pre-fix evidence at 2026-11-05 08:48 London: opening summary has 15 first
calls in five sessions and one recorded +60-minute result; the call list shows
zero. Daily closing results are present. The fix exposes existing records only;
the genuine +15/+60 capture gaps remain missing, with no database rewrite or
strategy/profile change. Exact publication and post-fix native parity evidence
are recorded in the pull request and the private operational audit artifacts.
