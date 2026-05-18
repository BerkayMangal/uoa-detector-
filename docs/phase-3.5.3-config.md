# Phase 3.5.3 — historical download (runbook)

This runbook describes the **bulk download** that replaced the
per-contract approach in Phase 3.5.3.9. The per-contract driver
(`scripts/download_tier2.py`) issued ~21,000 HTTP calls per
ticker-month and never finished in 11 days of wall-clock; it is
retained only for reference. The live path is `scripts/download_bulk.py`.

## Why bulk

ThetaData v3's `trade_quote` history endpoint accepts `expiration=*`
with no `strike`/`right` params and returns the **whole option
chain** for a symbol on a given date in one response — each trade
row carrying its at-trade bid/ask inline:

```
GET /v3/option/history/trade_quote?symbol=AMD&expiration=*&date=YYYYMMDD
  → {"response": [
       {"contract": {"symbol","expiration","strike","right"},
        "data": [{trade_timestamp,price,size,...,bid,ask,bid_size,ask_size}, ...]},
       ... one wrapper per contract ...
     ]}
```

A ticker-day costs **one** HTTP call instead of ~1,000. The full
universe is ~6,000 calls — hours, not weeks. The separate `quote`
bulk endpoint is deliberately NOT used: an `expiration=*` quote
bulk returns every quote tick of the whole chain (gigabytes, hangs
for tens of minutes). `trade_quote` returns only trade-count rows.

Because it is now hours rather than weeks, the download is
**agent-run**, not operator-run — this inverts the original
runbook's premise.

## Configuration

**Universe**: 24 tickers — `data/universes/_bulk_all.csv`
(`tier1_reduced.csv` 9 pure-equity names + `tier2_top15.csv` 15
names). ETFs and CVX were dropped from Tier-1; the gamma-squeeze
thesis is an equity-flow thesis and ETF flow has different
mechanics.

**Period**: 12 months — **2025-05-01 → 2026-04-30**.

**Filter**: `--max-dte 60` (Track B penalises 60+ DTE to zero in
v5_gamma_squeeze; downloading them is dead weight). The filter is
applied per (contract, day) after the bulk response lands.

**Concurrency**: 3. The bottleneck is CPU — JSON-decoding and
per-row Pydantic construction of multi-million-row responses, all
GIL-serialised. Concurrency 6 thrashed (slower per-call than
concurrency 1); concurrency 3 overlaps network wait with parse
without thrashing.

Total ticker-months: 24 × 12 = **288**.

**Output layout**: `data/historical/bulk/{TICKER}/{YYYY-MM}.parquet`
— the ticker-keyed layout `ParquetReplaySource` (Phase 3.5.5)
expects. Rows are sorted ascending by event timestamp before
write so the files satisfy the replay harness's
event-time-monotonic invariant.

## Command

```bash
cd /Users/berkay/Documents/uoa-detector-
set -a; . ./.env; set +a

# Theta Terminal v3 must be up on port 25503
java -jar ThetaTerminalv3.jar    # separate tab, keep alive

PYTHONPATH=src .venv/bin/python scripts/download_bulk.py \
  --tier-csv data/universes/_bulk_all.csv \
  --start-date 2025-05-01 --end-date 2026-04-30 \
  --output-dir data/historical/bulk \
  --max-dte 60 --concurrency 3 --log-level INFO
```

## Resume / failure semantics

| Situation | What happens |
|---|---|
| Process crashed / killed | Re-run the same command. A ticker-month whose parquet already exists with > 0 rows is skipped (`_month_complete`). |
| One day's fetch fails | Logged + skipped; the month still writes with the remaining days. The whole month is NOT failed for one bad day. |
| HTTP 472 (no data) | Treated as an empty success by the client — a non-trading day or an inactive chain. Not an error. |
| Circuit breaker | Disabled for the download (`circuit_breaker_threshold=10**9`) — a transient ThetaData blip must not cascade-fail the run. |

## Closeout (after the download finishes)

The parquet data is gitignored (`data/historical/**/*.parquet`).
The closeout commit records the universe CSV and this runbook —
not the data itself.

```bash
git add data/universes/_bulk_all.csv docs/phase-3.5.3-config.md
git commit -m "Phase 3.5.3: bulk download closeout"
git push origin phase-3
```

Then Phase 3.5.5 (real-data 4-cell backtest) becomes unblocked —
see `docs/phase-3.5-acceptance.md` §3.5.5.

## Reference — superseded per-contract config

The original plan (`download_tier2.py`, Tier-1 anchor 20 +
Tier-2 starter 51, 18 months, 13-18 day estimate) is preserved in
git history at commit `941cf2c` and earlier. It is not the live
path; do not run it.
