# Phase 5.48 closeout — clear daily workflow

Implementation and local verification complete; production delivery remains a
separate gate. Owner confirms the site is visible and authorizes continuation.

Home now starts with one recorded opening-policy decision: wait, inspect held
risk first, or inspect current entry plans. It explains the next checkpoint,
shows preparation/open/+15 times and the latest durable record, groups up to
three existing WAIT reasons and links to full plans/history. Current entries
and their next-action box reuse the existing expiry guard. Missing, old, closed
and pre-open data cannot be promoted from a green legacy source card.

Four principal navigation links retain all other tools in a native details
menu. Optional alert controls remain mounted inside their fold. Both legacy
cards and the large source board are folded together; their source-ready count
is scoped there. Held-risk notices stay visible. Section refresh preserves
expanded source/PAPER/diagnostic/method folds; client age warnings stay outside
the research fold. Home projections share one as-of time and the existing
opening read. No new HTTP writes or vendor calls.

Validation: **5319 passed, 52 credential-gated skips**, strict mypy **276 files**,
ruff/lock/diff clean. Nineteen new cases cover the actual calendar phases,
current/expired/stale/unrecorded entries, held-risk priority and scope, review
of stale exits, holiday/unknown next time, deterministic reason counts and an
authenticated real route with a legacy BUY but no opening decision and no writes.
All original tests/assertions, profiles and frozen strategy/research/artifact
sources remain unchanged. CSS rebuilt with the pinned Tailwind command.

Actual Chromium: 1440px/390px, pre-open/current/stale/risk fixtures, one h1,
no horizontal overflow, keyboard menu/all secondary routes, **zero axe violations
in eight views**, zero JS errors. Detailed plans return 200 for pre/current/stale.
Expanded research and the alert controller survive real GET refresh; expired
entry headlines are removed even when refresh fails. Screenshots visibly label
the synthetic local data. No production login or provider credentials used.

Judgments: opening policy remains the entry authority; stale held-risk messages
remain inspection prompts; three reasons are a presentation limit, not a trading
threshold. The pure helper lives outside the hashed opening/research packages.
No new calibration, fill, outcome or validated-edge claim.

Release: green PR/main CI plus matching actual deployed SHA/assets required.
If Railway again rejects the real feature release, retain the working preview
and report the limitation without another blind retry or owner panel task.
Next: actual delivery, then observe real recorded decisions and reference
outcomes under the unchanged policies.
