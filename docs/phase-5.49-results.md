# Phase 5.49 — frozen daily-proxy results

EXPLORATORY, NO VALIDATED ALPHA, NO LIVE PROMOTION. Calculated once from the
registered Study F source with report hash
`b32a840ebdf92e31140385300e367a68b558bb4fc897372a2e73572c2c9e50da`.
The archived panel has already been searched. No window below is an untouched
confirmation set, and NFLX is not in this historical panel.

## Primary result

Each row is an independent $10,000 cash portfolio, primary three-session hold,
5bp adverse slippage per side, whole shares and four slots. Dollar values are
net of that modeled slippage, before commission/tax. Windows reset capital.

| Reused window | Arm | Closed | Net USD |
|---|---|---:|---:|
| Development: 2025-09-18–2026-03-19 | Baseline | 104 | -1,111.89 |
| Development | Persistence | 88 | -1,999.99 |
| Development | Same-delay control | 104 | -11.68 |
| Development | Retest | 99 | -1,990.74 |
| Review: 2026-03-30–2026-06-18 | Baseline | 48 | +1,763.93 |
| Review | Persistence | 43 | +1,122.61 |
| Review | Same-delay control | 48 | +773.91 |
| Review | Retest | 44 | -696.51 |
| Audit: 2026-06-30–2026-09-18 | Baseline | 56 | -391.82 |
| Audit | Persistence | 51 | +882.36 |
| Audit | Same-delay control | 52 | +552.71 |
| Audit | Retest | 52 | -688.32 |

Retest is negative in all three primary windows and worse than its same-delay
control in each. This frozen daily proxy does not support adding that rule to
live decisions. A different horizon or cost result is not another selection
chance; all 144 fixed combinations remain visible.

Persistence is not stable across windows: it loses nearly $2,000 in development
and improves on baseline only in the audit window. Its audit +$882.36 becomes
-$63.86 after removing its single largest win, -$713.98 after removing its best
three wins, and -$234.35 at 50bp per side. These removals use a fixed ledger,
without reallocating cash. Its audit SPY/QQQ matched differences are +$581.44 /
+$647.50; these are same-allocation reference comparisons, not full-capital
buy-and-hold returns. The descriptive audit SPY daily-excess 95% interval is
[-0.4812, +0.7912] percentage points/day and spans zero. It is not a fresh test.

## Scope and reproduction

`uv run python -m scripts.replay_call_research --output <empty-directory>` runs
exactly four arms x three horizons x four costs x three windows = 144 portfolios.
The CLI refuses to overwrite evidence. Numeric decisions were frozen before
returns; no parameter was retuned or winner promoted after these results.

`artifacts/call-research-v1/report.json` is the compact read-only summary;
`replays.jsonl.gz` holds every trade/equity path. `manifest.json` separately
counts 288 fixed-ledger removal diagnostics and 24 descriptive intervals of
1,000 draws each. Draws and repeated portfolio trades are not independent units.
Independent source-price/cost/cash/reference arithmetic checks reconcile all
144 ledgers, including same-day cash ordering and four-slot/no-overlap bounds.

This is a daily price proxy. It cannot replay intraday +15 timing, actual UOA
aggression, historical IV/spreads or executable option P&L. Corporate-action
adjustment quality and survivorship remain unverified. Reserved Study G prices
were not read. No local production NFLX call or actual manual fill was available.

The software separately prepares prospective shadow evidence starting with
signals on/after 2026-11-02, preserving all four arms and recording actual source
availability and post-commit publication before entry. These independent daily
reference units are not the historical cash portfolio or new live BUY advice.
Actual observations and deployment must be verified separately.
