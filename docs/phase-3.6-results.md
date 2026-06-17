# Falsification verdict

Backtest period: 2025-05-01 → 2026-04-30

## Verdict: **EDGE REJECTED**

REJECTED — S4: best cell (tier2_fusion) Sharpe=0.54, walk-forward=0.38.

## Cells

| cell | closed trades | Sharpe | walk-forward | state |
|---|---|---|---|---|
| `tier1_single` | 0 | n/a | n/a | INSUFFICIENT |
| `tier1_fusion` | 956 | -5.292 | +0.25 | computable |
| `tier2_single` | 0 | n/a | n/a | INSUFFICIENT |
| `tier2_fusion` | 253 | +0.541 | +0.38 | computable |

## Triggered reject scenarios

- Scenario 4: best cell Sharpe < 0.5 OR walk-forward consistency < 0.75

## Scenarios not applicable

- Scenario 3: one side of the comparison was INSUFFICIENT.

## Recommended next steps

- The strategy as specified does not survive the falsification framework on this data. Per Phase 3.5 D4, do not retune thresholds on the same data. Options: revise the strategy hypothesis (a new sub-phase), enrich the data (e.g., longer window, additional axes), or end this strategy track and pivot.
