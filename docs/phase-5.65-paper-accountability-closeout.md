# Phase5.65 — PAPER audit and home visibility

Implementation verified; exact feature/main CI and native release evidence is
appended to the PR after normal publication. The accounting audit is in
`docs/phase-5.65-paper-accountability-audit.md`; no validated alpha is claimed.

## Result

All82 actual priced stock closes reconcile against immutable fill/exit events:
23 positive/59 negative, net-2880.55USD. Home's terminal slice had19 losses and
1 unfilled record. Separate full/recent counts expose the denominator rather
than implying every historical trade lost. The fixed-clock inverse diagnostic
is+2634.26USD after modeled slippage/before short-specific costs, a post-hoc
reuse of the same22 source days, not a promoted short/PUT strategy.

Home now exposes London entry/exit/mark clocks, classifies finite same-ET-day
marks within the existing900s limit as current, and labels other recorded marks
as historical, stale or unverified. Legitimate zero option marks/exits remain
visible. Unfilled PAPER is labelled unfilled. Only priced closed records show
a closing net. Old prices, realised outcomes, all rows/actions/navigation and
alerts remain intact. No provider request, DB migration or strategy change.

The new shadow method/profile/code identity is unchanged. Actual native reads
before this display release show its original10:59:39.655387Z activation and
4 real candidates, all entry/+15/+60 own/SPY pairs observed,0 invalid records
and no halt. These forward observations are separate from old stock PAPER and
the four old option replay records.

## Verification

- Unchanged full gate:**6434 passed,52 key-gated skips,two existing warnings**,
  201.27s; strict mypy349 files,Ruff,lock and whitespace checks pass.
-18 new tests cover SQLite's naive persistence clock boundary, fresh/stale/
  missing/future/pre-entry/nonfinite marks, ET-day rollover, zero option value,
  BST/London display, historical closed value/PnL, full/recent counts, null
  outcomes and authenticated SELECT-only HTML rendering with no writes.
- Existing results/position25-case regression passes unchanged. Separate
  current/legacy accounting188-case focused audit passes; original assertions
  and all frozen profiles/research artifacts are preserved.
- Independent50-digit Decimal reconstruction matches all82 mirror rows, the
  double-cost identity and the aggregate0.05201USD fill-rounding bound.
- Chromium1440/390/320 synthetic seven-row fixture passes with preserved rows,
  fresh/stale/future/missing/zero/unfilled/closed states,15 navigation links and
  one alerts controller/script. No page overflow/browser exceptions; narrow
  tables retain contained horizontal scrolling. Fixture prices are synthetic.

## Limits

Accounting agreement does not establish raw provider accuracy or executable
fills. Correlated/overlapping source positions are not independent bets. The
old option replay's exact-threshold EOD stop assumption flatters one example
by14USD; its later-mark/earlier-exit display is a separate legacy limitation.
Neither is silently repaired or pooled into current totals. Correcting frozen
replay execution assumptions requires a separate measurement protocol.
