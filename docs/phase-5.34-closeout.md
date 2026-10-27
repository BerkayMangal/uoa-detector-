# Phase 5.34 closeout — exploratory calibration

KAPALI — frozen bounded search ran; one EXPERIMENTAL_FORWARD_ONLY candidate is
registered for prospective, independent PAPER measurement. No live-policy
promotion, measured alpha or order execution. Owner authorizes merge/deploy in chat.

## Evidence

Only the already-reused Study F panel: 17,640 rows, 70 names, 252 sessions,
2025-09-18 through 2026-09-18. No Study G prices were loaded. All historical
splits, including the internal test, remain exploratory, not untouched evidence.

- 1,728 unique configurations, 3,461 distinct portfolio evaluations: train and
  validation for every configuration, then one selected primary test, two
  secondary horizons and two cost sensitivities.
- 63 passed training; 6 passed training plus validation. Candidate
  `4c244944e3ba4e6d` was selected by validation SPY excess, not test performance.
- Parameters: 20-session lookback, lag >=4 percentage points behind SPY,
  close-minus-open recovery >=0 prior ATR, volume >=1x previous-20-session mean,
  SPY above MA20, close change <=0.5 prior ATR. Primary hold remains 3 sessions.

| Split | Closed | Net model dollars | Mean matched SPY excess (pp) | Close-marked max drawdown |
|---|---:|---:|---:|---:|
| Train | 80 | +496.33 | +0.1140 | 19.46% |
| Validation | 67 | +2406.37 | +1.2894 | 4.46% |
| Internal test | 41 | +950.33 | +1.2036 | 6.28% |

Test: no missing entry prices or unresolved positions; 41/41 SPY matches.
Each split independently starts at $10,000. These numbers are not stitched
returns or an annual return. SPY comparison is a trade-weighted matched return,
not an executed hedge or cash-portfolio excess return.

One-session diagnostic: +$590.17, 51 trades; five-session diagnostic: **-$103.93**,
30 trades. The negative five-session result is visible outside the UI fold.
10bps/side test sensitivity: +$925.81, 40 trades; 20bps/side: +$755.54, 38 trades.
Higher costs change affordable quantities and later cash availability, so the
executed historical trade sets can differ. No horizon/cost result changes the
selected parameters. Fees/tax, liquidity impact, dividends and intraday stop
sequencing are not modelled.

## Auditable artifacts

- `profiles/calibration_v1.yaml`: frozen grid, budgets, dates and selection gates.
- `artifacts/calibration-v1/report.json`: complete grid scores, selected test
  ledger, diagnostics, calendar, source/profile/code/report SHA-256 hashes.
- `artifacts/calibration-v1/selected-ledger.json`: deterministic selected-candidate
  replay of all three splits, 188 trades, exactly matching frozen metrics. This
  repeats 3 existing evaluations for audit; it adds no selection trial.
- Report hash:
  `e272b8f64a6af28ba4c947f9e2d6027ab68b9a03eda8c7af9534222f78e2b66b`.
- Data hash:
  `ed1f6d8871e900f959793edb8ed7da4b52a1502da3c7974e2279e74e6a7205ca`.

An initial, unreported run was interrupted before inspecting any results to
cache repeated immutable calendar parsing. It wrote no artifact. The same frozen
grid/gates were then completed once; no threshold or hypothesis was revised.

## Future PAPER and UI

Private `/kalibrasyon`, linked from Alfa lab, shows historical evidence and
separate future records. Prospective signal dates begin **2026-10-28 ET**.
Settled OHLCV is captured from the existing daily-close payload, with no new API
requests. Three new append-only tables retain first observations, pre-open
instructions and exact-session entry/exit results. Missing/contradictory data
stay missing; a late observation cannot manufacture a past decision. Original
positions, PAPER policies and study verdicts remain unchanged.

Future records each have an independent $2,500 unit budget and whole shares,
5bps/side model slippage and a matched SPY observation. They are not the
historical cash-constrained portfolio; their totals are labelled accordingly.
No candidate is a broker order, automatic live BUY or alert-policy promotion.
Coverage is the actual subset of the fixed universe already being fetched.
Missing names are visible; this release does not spend extra quota to fill them.
PAPER returns necessarily await future sessions; none is claimed now.

## Decisions and limits

1. Literal split dates in the frozen contract/profile take precedence: there
   are **six complete omitted exchange sessions** between each pair of windows,
   rather than the prose's five. This exceeds the five-session purge requirement;
   all dates remained fixed before the search and no label crosses a boundary.
2. Daily settlement capture uses the existing conservative legacy finality rule.
   An early-close bar may be recorded later that evening; it is never treated as
   final before that rule permits it. The next-session instruction must still
   exist before the actual declared regular open.
3. Test success freezes only an experimental hypothesis. Reused data, selected
   name universe, potential corporate-action/adjustment errors, dependent trades,
   small sample and 1,728 trials preclude a validated-alpha claim. No p-value or
   optimized win probability is displayed. Failed historical studies stay failed.
4. Search runs only via an explicit offline CLI; no HTTP optimizer. Monitoring
   is off-loop, timed, single-flight and rate-limited, and failures preserve the
   desk. Code/profile/report hash mismatch blocks its use.

## Verification

- Hermetic gate: **4908 passed, 52 skipped**, +33 cases (18 new behaviour tests,
  15 route-auth cases). Strict mypy: **246 source files**; ruff, lock and diff clean.
- Tests exercise prefix causality, gap/calendar handling, prior ATR/volume,
  boundary purges, whole quantities, cash timing, exact missing exits, selection
  independent of future prices, small/missing/failed metrics, immutable settled
  capture and conflicts, prospective pre-open decisions, no late fills, unit
  budget nonfills, hash tampering, failure isolation and route privacy.
- Chromium on real frozen report plus labelled local database fixture: 1440px
  and 390px, all folds open/closed, no overflow, no JS errors, zero WCAG A/AA axe
  violations. Screenshots in `/workspace/uoa-design-review/calibration-*` outside
  the repository. Production-authenticated inspection is unavailable here;
  public release SHA and route protection are verified after merge.

No scoring module changed; score truth tables do not apply. Next evidence is
future PAPER coverage/results, not another pass over these historical thresholds.
