# Phase 3.6 — closeout

**Status: CLOSED. Verdict: EDGE REJECTED — no robust UW-free edge.**

## What Phase 3.6 set out to do

UW went unresponsive for ~3 weeks, blocking the Track B verdict (the
fusion enrichment needed UW historical data). Phase 3.6 asked: can we
reconstruct the confluence enrichment from the ThetaData option chain we
already own, and get a real verdict **without** UW — same thesis (edge
requires multi-source confluence), only the data source behind the
enrichment changes?

The answer the phase produced is **no robust edge**, delivered honestly.

## The verdict

5-axis self-derived confluence (`v6_thetadata_confluence`, the leak fixed):

| cell | closed | Sharpe | walk-forward | E |
|---|---|---|---|---|
| tier1_fusion | 993 | -5.42 | 0.12 | -0.042R |
| tier2_fusion | 257 | +0.63 | 0.38 | +0.0051R |
| tier1/2_single | 0 | — | — | INSUFFICIENT |

Scenario 4 fires: best cell (tier2_fusion) clears the 0.5 Sharpe bar but
its walk-forward consistency is **0.38** — positive in 3 of 8 quarters,
**below random**. A few windows carry a marginally positive average; most
lose. That is the non-robust / overfit-prone signature, mechanically
**REJECTED**.

The pattern across the build, the central finding:

| config | tier2 Sharpe | walk-forward |
|---|---|---|
| gamma + core | -0.76 | — |
| + IV / OI | -0.76 (unchanged) | — |
| + price (0.10) | +0.54 | 0.38 |
| + catalyst (0.15) | +0.63 | 0.38 |

**Weighted axes nudge the average (Sharpe, expectancy) but not the
consistency (walk-forward).** Adding axes did not move the binding
constraint. The inconsistency is fundamental, not a missing-axis problem;
the last untested axis (sector, 0.05) would not change it.

## The critical finding — a fake edge, caught

The first v6 run came back **EDGE PROVEN, Sharpe 4.9** — implausibly smooth
for a convexity strategy. We did not believe it. A trade-level audit found
**84% of closed trades had `exit_ts <= entry_ts`** (negative holding
periods): `SimplePnLProvider`'s floor exit (`expiry - exit_on_dte_lte`)
landed before entry for sub-floor-DTE signals, and `get_bid` walked back
to a **pre-entry** quote — a look-ahead leak fabricating PnL. That leak was
the entire "edge". Fixed in `b5fcd7a`; the edge vanished (-0.044R).

This leak affected the **whole replay PnL path**, not just v6 — a future
UW-fed Track B verdict would have been inflated the same way. Catching it
is the most valuable outcome of the phase.

## Methodology learnings (pin these)

1. **Not every module is a weighted confluence axis.** `compute_combined_
   score` weights: uoa, convexity, event, gamma, price_confirmation,
   sector_confirmation, cluster, time_of_day (+ ScoreAdjustments). M24 (IV)
   is only a conditional *penalty*; M27 (OI) feeds the deferred M28, not
   the score. Wiring them changed nothing — verified by an identical
   verdict. Always confirm an "axis" actually enters the score.
2. **Walk-forward is the honest robustness signal; Sharpe is noisy.**
   Pseudo-replication (many correlated same-day trades) inflates the
   daily-aggregated Sharpe. The walk-forward consistency (0.38) is the
   metric that did not lie.
3. **Be most skeptical of the best-looking result.** Sharpe 4.9 was the
   tell; the audit found the bug. The sanity-audit discipline (3.5.7)
   earned its keep.

## What was built (reusable, UW-independent, all tested)

`src/uoa_detector/sources/thetadata_derived/`: GEX dealer-positioning
(BS gamma + zero-GEX flip), IV-history + OI providers, price-action
(intraday spot), CSV catalyst calendar; `black_scholes.py`. Precompute
scripts: `download_chain_snapshots.py` (full-chain OI + eod → BS-inverted
IV), `compute_spot_series.py`, `fetch_earnings_calendar.py`. Wiring:
`fusion_stages_with_thetadata` + `run-4cell --chain-snapshots
--spot-series --catalyst-calendar`; profile `v6_thetadata_confluence`.
All point-in-time correct (as-of guards), all green.

If UW historical access ever lands, the self-derived-vs-UW comparison is
one flag away — and the leak fix makes either verdict trustworthy.

## Commits

`e07260e`/`d351eec`/`22dbf56` acceptance · `4a8f946` 3.6.1 GEX ·
`0d5b4f0` 3.6.2 snapshots · `2c96de2` 3.6.3 wiring · `073218c` first
verdict · **`b5fcd7a` leak fix** · `673e5d2` 3.6.4 IV/OI · `5aa9596`/
`20d5988` 3.6.5 price · `f405cac`/`ad2374a` 3.6.6 catalyst.

Verified clean at close: `pytest` 1559 passed / 20 skipped, `mypy
--strict` clean, `ruff` clean.

## Addendum — the raw signal has no market-neutral edge either

After the confluence verdict, we tested the rawest form of the thesis,
bypassing the pipeline entirely: do large aggressive ISO sweeps (≥$100k,
fill at/above ask) predict the underlying's direction? Forward spot
returns, signed by option type.

First pass looked like a find — call sweeps showed +0.97% direction-
aligned 5-day return (hit 54%, de-replicated t-stat 2.76). But the
skeptical control killed it: **subtract the universe-mean forward return
(market drift) and the call-sweep EXCESS return is -0.046%, t-stat
-0.20 — zero.** The "edge" was the 2025-26 up-drift; the aligned-return
sign convention made "calls are long in a rising market" look like
prediction. Put sweeps: +0.30% excess, t-stat 1.24 — also insignificant.

So **the unusual options flow in this data has no market-neutral
directional edge** — not as confluence, not as raw aggressive flow.

Second fake edge caught this phase (after the look-ahead leak's Sharpe
4.9). The lesson holds: be most skeptical of the best-looking result, and
control for the obvious confound (here, market beta) before believing it.

## Verdict and next step

**Track B (gamma-squeeze confluence) does not have a robust edge derivable
from ThetaData alone.** The thesis bet on UW confluence; the UW-free
reconstruction does not clear the falsification bar. This is a real answer
to the binary question, not a tuning failure.

Phase 3.6 is frozen here. The next strategic move — re-pursue UW historical
access for the original Track B test, revise the hypothesis, or pause — is
Berkay's call (per CLAUDE.md, a rejected verdict starts nothing new without
direction).
