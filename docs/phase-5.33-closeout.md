# Phase 5.33 closeout — position desk

KAPALI — position monitoring and analysis implemented; no broker orders and no
claim of measured alpha. Owner's chat authorizes PR merge and production deploy.

## Delivered

- Private `/pozisyonlar` separates active PAPER from open journal-recorded real
  trades. Known entry outlay, planned risk, missing-risk counts and committed
  basket concentration are explicit; pending PAPER consumes no funded total.
- Append-only underlying stop/target revisions for journal trades. Crossing means
  exit review, never a real sale. Unsupported short shares remain unknown.
- Opt-in exit notifications cover recent PAPER closed/price-pending and recorded
  manual-plan crossings, independently of the entry CALL-only preference.
- Prospective append-only entry/exit reference observations. SPY and all frozen
  peers must satisfy freshness, availability and pair-skew checks; old entries
  remain missing. Comparisons are underlying percentage points before costs.
- Retained optional last-tape IV; 1/3/5-session stock/option scenario grid uses
  labelled European modelling, fixed future spread, whole quantities and costs.
  Mid-model IV is explicitly distinguished from fresh transaction IV.
- A read-only current-price guard withdraws a historical BUY whose underlying
  no longer satisfies its stored stop/chase plan. Immutable decisions and all
  existing strategy profiles remain unchanged; no additional vendor requests.
- Mobile result tables are keyboard-focusable scroll regions.

## Verification

- Hermetic suite: **4875 passed, 52 skipped** (59 added cases: 29 new behavioural
  cases plus 30 automatically enumerated authentication checks).
- Strict mypy: **239 source files**; ruff and locked dependency checks pass.
- Chromium, actual application plus labelled SQLite/provider fixtures: `/`,
  `/pozisyonlar`, `/sonuclar` at 1440px and 390px, no page overflow or WCAG
  A/AA axe violations. Screenshots outside the repository in
  `/workspace/uoa-design-review/positions-*`.
- Browser exit delivery: PUT exit arrives with CALL-only entry preference;
  repeats suppressed, disabled exits silent, re-enable baselines existing exits.
  One actual AudioContext survives section refresh. Unsaved plan values survive
  edits both before and during a pending refresh response.
- Production credentials are absent here: authenticated screens were verified
  locally, not represented as production browser inspection. Public health SHA,
  Railway status and unauthenticated route protection are checked after merge.

## Decisions and limits

1. The $10,000 PAPER capital is a labelled display reference, not an enforced
   portfolio budget. Existing independent PAPER records are not reinterpreted
   as an executable portfolio. Manual/PAPER dollar totals never mix.
2. Current cached underlying and recent option close marks are sampled prices;
   neither a stop order nor a guaranteed maximum loss. Missing marks stay unknown.
3. Option scenarios anchor model changes to the observed mid and assume current
   half-spread at exit. They are hypothetical values, not probabilities, forecasts
   or American-option/early-exercise valuations.
4. Repeated manual observations share position/plan/reason notification identity;
   a plan revision allows a distinct subsequent review. Closing the journal trade
   suppresses its notices; no observation edits that trade.
5. Captures run off-loop with timeout and a single-flight lock. Exceptions retain
   legacy entry processing and expose only safe job status.

No scoring module or historical study verdict changed; score truth tables do not
apply. Next: phase 5.34, bounded exploratory calibration and frozen prospective
assessment, keeping reused historical data explicitly exploratory.
