# Phase 3.5.5 — status, full diagnosis & finish plan

Status 2026-05-18. This is **not** a Phase 3.5.6 verdict. It is the
complete, measured diagnosis of why the real-data backtest cannot
produce one yet, and the scoped plan to get there.

## What is done and works

| Item | Commit | State |
|---|---|---|
| Bulk historical download — 24 tickers × 12mo, 245.9M rows | 3.5.3.9–11 | Complete, verified. `data/historical/bulk/`. |
| `ParquetExitQuoteProvider` | 3.5.5.1 | Complete + tested. |
| `replay_trade_producer` + `run-4cell --trades replay` | 3.5.5.2 | Complete + tested. |
| IV-less / OI-less prints through fusion | 3.5.5.3 | Complete. |
| M37 median provider wired | 3.5.5.4 | Complete. `data/medians_bulk.csv`. |

The replay pipeline runs end-to-end on real data. The problem is what
it produces.

## Measured result (JNJ 2025-07, single cell, 114,913 events)

| Signal | Result | Should be |
|---|---|---|
| `combined_score` | max **0.150**, p50 0.135 — flat | varies; some cross 0.55 |
| events ≥ 0.55 threshold | **0** | a small unusual fraction |
| `uoa_score` | **0.500 for every event** (constant) | varies by aggression |
| `convexity_score` | **never computed** (None) | varies by moneyness/DTE |
| `fill_side` | **"unknown" for all 245M prints** | above_ask / below_bid / ... |
| `is_iso` | **False for all prints** | true for ISO sweeps |
| `spot_price` | **0 for all prints** | the underlying price |

The detector currently sees nothing: its two namesake axes (UOA,
convexity) are flat/empty, so no event is tradeable.

## Root causes — why each signal is dead

1. **`fill_side` = "unknown"** — the ThetaData mapping hard-codes it
   (`mapping.py:639`, "M34 classifies; mapping doesn't infer"). But
   M34 does **not** infer it either — it only *reads* `fill_side`.
   So aggression (the core UOA signal) is never derived. It is
   derivable: trade `price` vs `bid`/`ask`, all present in the data.

2. **`uoa_score` = 0.500 flat** — M34 sets a 0.5 baseline and adds a
   bonus only for `iso` or `sweep`. `is_iso` is hard-coded False
   (`mapping.py:643`); `sweep` needs `source_agreement.exchanges_seen
   >= 2`, but the single-source fast path emits one event per print
   with one exchange. So every event is classified `block` → no
   bonus → flat 0.5.

3. **`convexity_score` = None** — convexity needs moneyness (strike
   vs spot). `spot_price` is 0 because ThetaData's `trade_quote`
   feed carries no underlying price, and the ThetaData STOCK data
   add-on ($80/mo) is not subscribed (stock endpoints return 403).

4. **M21–M27 enrichment** — the UW providers fetch *current* data and
   ignore the event timestamp (`dealer_gamma.py`: `del at`). Wiring
   them into a 2025 replay = lookahead bias. Live-only by design.

5. **Scale** — pipeline ≈ 1,000 events/s; 245.9M events ⇒ days per
   pass, and the in-memory store holds every signal ⇒ OOM.

## The real situation

The detector code is built and unit-tested (1,400+ tests), but it was
architected for a **live, multi-source, fully-enriched** environment.
It was never adapted to **backtest from a single historical option-
trade feed**. Phase 3.5 "run the backtest" actually requires
re-deriving the detector's input signals for the historical-replay
context. That is a real engineering phase, not a command.

## Finish plan (scoped, ordered)

### Track A — single-cell UOA + Convexity verdict (ThetaData only)
Answers "is there edge in raw unusual flow + convexity" — the
product's namesake. Achievable with data in hand + one $80 add-on.

| Step | Work | State |
|---|---|---|
| A1 | Derive `fill_side` from price vs bid/ask in the mapping | **DONE** — commit 3.5.5.5 |
| A2 | Decode `is_iso` from ThetaData trade condition codes (95/126/128) | **DONE** — commit 3.5.5.6 |
| A3 | Bucket cross-exchange near-simultaneous prints so M34 sees multi-venue sweeps | ~2–3 days |
| A4 | Spot price: subscribe ThetaData STOCK add-on ($80/mo), download stock history, join → unblocks convexity | ~1–2 days |
| A5 | Candidate pre-filter + streaming store for the 245M-row scale | ~2–3 days |
| A6 | Run single-cell 4-cell, Phase 3.5.6 falsification | ~1 day |

**Track A remaining: ~1–1.5 weeks → a real verdict on UOA + convexity edge.**

**Re-download note:** A1 and A2 are mapping-layer fixes — they take
effect only when the bulk data is re-downloaded (the parquet stores
`fill_side`/`is_iso` but not the raw condition code). A4 also adds a
mapping change (spot from a stock-history join). To avoid spending
ThetaData bandwidth twice, the re-download is **batched once after A4**,
capturing A1 + A2 + A4 together. A3 and A5 need no re-download.

### Track B — full fusion (8-axis confluence) verdict
Needs historical point-in-time UW data.

**B1 RESOLVED (probed live 2026-05-18).** The UW API *does* expose
historical endpoints with a `date` param — but the current
subscription only returns the **last 7 trading days**:

> `historic_data_access_missing` — "The earliest date currently
> available to you is 2026-05-07 (7 trading days) ... If you wish to
> access full historic data please email dev@unusualwhales.com."

The backtest window is 2025-05 → 2026-04 (12 months). **UW historical
enrichment data for that window is not accessible under the current
plan.** This is the single hard gate on a real Phase 3.5 verdict.

| Step | Work | Est. |
|---|---|---|
| B0 | **Obtain UW full historical data access** — email dev@unusualwhales.com, likely a paid tier / data-shop purchase | external, Berkay |
| B2 | Rebuild the 6 UW providers in "as-of" mode (they fetch dated series already — just need to *select* by `at` instead of `del at`) | ~3–5 days |
| B3 | Wire enrichment into fusion cells, run full 4-cell | ~2–3 days |

**Track B total once B0 is granted: ~1.5–2 weeks.**

## The bottom line

There is **no single-cell shortcut.** `v5_gamma_squeeze` is a
confluence strategy by construction: the 0.55 threshold is set so
that no single axis and no core-only subset clears it — that *is*
the Track B hypothesis ("edge requires confluence"). Measured:
core-only `combined_score` tops out ~0.17 even with A1–A3 done.
So the single cells are the null baseline; a real verdict requires
the **fusion cells**, which require **historical UW data**.

## Decisions / actions needed

1. **UW full historical data access** — email dev@unusualwhales.com.
   This is THE gate. Nothing downstream produces a verdict without it.
2. **ThetaData STOCK add-on, $80/mo** — required for spot → convexity
   (Berkay is purchasing).
3. Track A1–A3 are done and are real input-signal fixes the fusion
   backtest will use; A4 (spot) waits on the add-on; A5 (scale) can
   proceed any time. But none of A4–A6 yields a verdict without B0.
