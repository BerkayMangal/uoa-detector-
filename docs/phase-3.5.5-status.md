# Phase 3.5.5 — status & remaining blockers

Status as of 2026-05-18. This is **not** a Phase 3.5.6 verdict — the
real-data 4-cell backtest cannot complete yet. This doc records the
work done, what runs, and the integration walls still in the way.

## What is done and works

| Item | Commit | State |
|---|---|---|
| Bulk historical download (24 tickers × 12mo, 245.9M rows) | 3.5.3.9–11 | **Complete.** `data/historical/bulk/{TICKER}/{YYYY-MM}.parquet`, all 288 files monotonic + schema-valid. |
| `ParquetExitQuoteProvider` | 3.5.5.1 | Complete + tested. |
| `replay_trade_producer` + `run-4cell --trades replay` | 3.5.5.2 | Complete + tested. |
| IV-less / OI-less prints through fusion | 3.5.5.3 | Complete. ThetaData v3 has no historical print IV/OI; `OptionsPrint` made nullable. |
| M37 median provider wired | 3.5.5.4 | Complete. `data/medians_bulk.csv`, 24 tickers. |

The replay pipeline runs end-to-end on real data: a ticker-month
streams through the core-flow stages, scores, and produces trades
via `SimplePnLProvider`.

## Why the 4-cell backtest cannot produce a verdict yet

Three walls, in order of severity:

### 1. Fusion cells need historical Unusual Whales data — none exists

The M21-M27 enrichment stages (dealer gamma, dark pool, sector flow,
IV regime, OI, events) are what make a "fusion" cell differ from a
"single" cell. Their UW providers were built for **live** detection:
they fetch **current** data and ignore the event timestamp. Verified
in `dealer_gamma.py`: `del at  # UW returns daily snapshots; date is
not part of the key`.

Wiring them into a 2025 replay would enrich 2025 events with 2026 UW
data — lookahead bias, a fabricated result. So the fusion cells
**cannot be validly run** without either (a) historical "as-of" UW
providers or (b) a pre-downloaded UW enrichment snapshot. Neither
exists. This is a real build, gated on whether the UW API even
exposes historical point-in-time data.

### 2. The score threshold is tuned for full confluence

`v5_gamma_squeeze.yaml` sets the signal threshold at 0.55. That
number assumes all 8 enrichment axes contribute. A single-cell
core-only run (no M21-M27) tops out around 0.115 even on genuine
prints — so it takes **zero positions**: every event is
`PENALIZED_BELOW_THRESHOLD`, `max_r=0.0`.

This is not "no edge" — it is a core-only pipeline measured against a
confluence-tuned bar. Lowering the threshold to make single cells
trade would be curve-fitting, which Phase 3.5 D4 forbids during
validation. So single cells report 0 trades → INSUFFICIENT.

### 3. Performance / memory at full scale

The pipeline processes roughly 1–5k events/sec. The dataset is 245.9M
events; a single full pass is many hours, and `BacktestStore` holds
every signal in memory (245M objects → OOM). A real run needs a
candidate pre-filter (most of 245M trades are 1-contract retail noise
that no UOA detector should score) and/or a streaming store.

## Honest verdict on Phase 3.5

Per the Phase 3.5.6 sample-size gate ("all four cells INSUFFICIENT →
edge unverified, not rejected"), Phase 3.5 currently lands on
**edge unverified** — the strategy is neither proven nor rejected;
the data/integration to test it is not yet in place. This is a
Phase 3.5.3-style follow-up, not a Phase 3.5.6 verdict.

## Recommended path

1. **Decide the fusion-data question first** — does the UW API expose
   historical point-in-time endpoints? If yes, rebuild the 6 UW
   providers in "as-of" mode. If no, a UW historical snapshot
   download (like the ThetaData one) is needed. This is the gating
   decision; everything else is secondary.
2. Add a candidate pre-filter to the replay (premium floor / size
   gate) so the pipeline processes ~1–5M candidates, not 245M.
3. Only then is a 4-cell run meaningful, and only then does the
   0.55 threshold get tested as designed.

The detector code is built and unit-tested (1400+ tests). What
Phase 3.5 has exposed is that the **integration layer** — feeding the
pipeline real, time-correct, full-coverage data at scale — was never
built. That is the actual remaining work.
