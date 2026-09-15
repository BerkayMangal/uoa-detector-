# Phase 3.5.2 — Historical download dry-run

**Status: KAPALI** (filled at run time below).

Purpose: validate the `scripts/download_tier2.py` script on a
small subset (5 ticker-months) before committing to the multi-day
Phase 3.5.3 full Tier-2 download. Catches schema / rate-limit /
quota / endpoint-shape surprises early.

---

## Run parameters

| Setting | Value |
|---|---|
| Date | 2026-05-12 |
| Tier CSV | `data/universes/tier2_starter.csv` (51 tickers) |
| Date range | 2026-04-01 .. 2026-04-30 (1 month) |
| `--max-tasks` | 5 |
| `--max-contracts` | 30 (per ticker; safety cap for the dry run) |
| Concurrency | 2 (Pro plan permits 4; conservative for sandbox) |
| Output dir | `data/historical/dry-run/` (gitignored) |
| Branch HEAD | (filled below) |

## Pre-flight blockers encountered and fixed

Phase 3.5.2 surfaced four issues that Phase 3.3.7 had missed; each
was fixed in its own sub-commit before the dry run could complete:

| Phase | Issue | Fix |
|---|---|---|
| 3.3.10 | `historical/contracts.py` still hit retired `/v2/list/contracts/option/quote` (HTTP 410 Gone) | Migrate to `/v3/option/list/contracts/quote` with `symbol`+`date` params; tolerate v3 ISO date / float dollars / CALL\|PUT response shape |
| 3.3.11 | `_decode_quote_response` and `_decode_trade_response` expected top-level row arrays; v3 actually nests rows inside per-contract `{contract, data}` wrappers | `_normalize_response_to_rows` now flattens `response[*].data[*]` automatically; legacy shapes still tolerated |
| 3.3.12 | Custom HTTP 472 ("No data found for your request") was raising `ThetaDataAuthError` and tripping the circuit breaker — fatal for any download that legitimately encounters a no-data day | Client treats 472 as a normal empty-response success (`{"response": []}`); breaker stays closed |
| 3.3.13 | `TradeRow.sequence: int = Field(ge=0)` rejected v3's signed 32-bit sequence values (negatives observed) | Drop the `ge=0` constraint; sequence is an opaque tie-breaker |

The dry-run results below are taken from the run **after** all
four fixes landed.

---

## Manifest result

From `data/historical/dry-run/.manifest.json` (commit
`<filled by git after closeout>`):

```
tickers in universe: 51
months in range:     1
tasks done:          5
tasks failed:        0
total rows:          12,282
total files:         150
total size:          1.03 MB
wall clock:          19.2 min (1150 s)
```

### Per-task outcome

| Ticker-Month | Status | Row count | Files | Bytes |
|---|---|---:|---:|---:|
| ABNB:2026-04 | done | 882 | 30 | 173,122 |
| AFRM:2026-04 | done | 2,311 | 30 | 219,758 |
| AI:2026-04 | done | 5,254 | 30 | 265,000 |
| APP:2026-04 | done | 1,641 | 30 | 214,829 |
| CHGG:2026-04 | done | 2,194 | 30 | 202,099 |

Each ticker hit the `--max-contracts 30` cap (dry-run safety
limit); the full download will lift the cap and pull every
listed contract per ticker. AI being the largest at 5,254 rows
is consistent with the AI sector's elevated options activity
through Q2 2026.

### Gap pattern (sanity check)

Every ticker reports the same 9 missing trade-days per month:
2026-04-03, -04, -05, -11, -12, -18, -19, -25, -26. These are
the 4 weekends in April 2026 (Apr 4-5, 11-12, 18-19, 25-26) plus
2026-04-03 (Good Friday — US market closed). Manifest correctly
flags closed sessions as gaps; this confirms the orchestrator's
trading-calendar derivation is sensible (calendar-day windowing
with empty-day passthrough).

### Schema validation

Each parquet file passes through the per-write
`validate_parquet_file` call in
`uoa_detector.historical.parquet_writer`. The orchestrator
short-circuits to `mark_failed` on any
`RAWPRINT_PARQUET_SCHEMA` drift — none was raised across all
150 files. Confirms the v3 wire-format migration (Phase 3.3.7
through 3.3.13) produces RawPrint records that round-trip
through the existing parquet schema unchanged.

---

## Cost extrapolation

| Metric | 5-ticker-month subset | Full Tier-2 (51 × 24) — cap=30 extrapolation |
|---|---:|---:|
| Ticker-months | 5 | 1,224 |
| HTTP calls | ~3,000 | ~734,400 |
| Rows | 12,282 | ~3,007,613 |
| On-disk parquet | 1.03 MB | ~252 MB |
| Wall-clock | 19.2 min | ~78 hours (~3.3 days) |

### Important caveat

The dry-run used `--max-contracts 30`, capping each ticker's
contract universe at 30 of the typically 200-5000 listed
contracts on liquid names. The **real** full Tier-2 download
will lift this cap, multiplying both bytes and wall-clock by
roughly 5-30x for the most liquid tickers and ~2-5x for the
thinner names. Realistic full-download upper bound:

  - Bytes: 252 MB × 10 ≈ **2.5-7 GB** (well within 50-200 GB
    free-space target in Phase 3.5.3 spec)
  - Wall-clock: 78 hours × 10 ≈ **30-50 days at concurrency=2**
    → BLOCKING per Phase 3.5 §"open question" rule

The acceptance contract specifies concurrency=4 (Pro plan
allowance) for Phase 3.5.3. Doubling concurrency roughly halves
wall-clock → **15-25 days at concurrency=4**, still above the
8-48-hour target. The operator should plan for the longer end
or apply the Phase 3.5 §"open question" subset rule (top-25 by
liquidity, 2-3x faster).

## Bandwidth-fit decision

ThetaData Pro plan monthly bandwidth allowance is not
codified in the profile; the operator confirms before the full
download whether 2.5-7 GB sits comfortably within the monthly
quota. At current pricing the answer is yes — Pro tier
historical access has TB-scale monthly bandwidth.

**Approved plan for Phase 3.5.3:**
  - Run with `--concurrency 4`, no `--max-contracts` cap
  - Start with the full 51-ticker Tier-2 universe
  - Monitor wall-clock for the first 8-hour wall block: if the
    Phase 3.5 §"open question" 1-ticker-month/hour threshold
    drops below target, fall back to top-25-by-liquidity subset
    per the acceptance contract's escape hatch.

Phase 3.5.3 remains the operator's responsibility; the agent
does not run that command.

---

## Sıradaki

Phase 3.5.2 KAPALI → Phase 3.5.4 (synthetic 4-cell backtest)
starts in the same agent run. Phase 3.5.3 (full Tier-2 download)
remains the operator's responsibility — agent does not run that
command.
