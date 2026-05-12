# Phase 3.5.3 — Tier-2 historical download (operator runbook)

**Operator-only runbook.** This is the configuration Berkay
applies for the Phase 3.5.3 full Tier-2 download. Agent does NOT
run these commands per the Phase 3.5 acceptance contract; the
agent's only job is to keep this doc accurate.

## Plan summary

**Universe**: Tier-1 anchor (20 tickers) + Tier-2 starter
(51 tickers) — see "universe note" below.

**Period**: 18 months — **2024-11-01 → 2026-04-30**.

**Filter**: `--max-dte 60` (Track B penalises 60+ DTE to zero
in v5_gamma_squeeze; downloading them is dead weight).

**Concurrency**: 4 (ThetaData Pro plan sustained allowance).

### Universe note

Reading `data/universes/tier1_anchor.csv` and
`data/universes/tier2_starter.csv` shows **zero overlap** between
the two universes (Tier-1 = mega-cap + ETFs; Tier-2 = mid-cap
niche). The Phase 3.5 acceptance doc's "Tier-1 is a subset of
Tier-2" assumption is incorrect with the current CSVs.
Consequence: both universes have to be downloaded separately;
they cannot be derived from one parquet pool.

Total ticker-month: **20 × 18 + 51 × 18 = 1278** (vs the full
Phase 3.5 acceptance spec of 51 × 24 = 1224 — essentially the
same wall-clock; we trade 6 ticker-months of breadth for 27
ticker-months of mega-cap depth and DTE-filter savings).

### Cost estimate

From Phase 3.5.2 dry-run: 5 ticker-months → 19.2 min on a single
concurrency=2 thread, no DTE cap, `--max-contracts 30`.

Scaling to the full Phase 3.5.3 config:

| Factor | Multiplier | Note |
|---|---|---|
| Drop `--max-contracts` | × ~10 (liquid names) | Real contract counts |
| Apply `--max-dte 60` | × ~0.5 | DTE > 60 filtered |
| Bump concurrency 2 → 4 | × ~0.55 | Pro plan headroom |
| Scale 5 → 1278 ticker-month | × 255.6 | Linear |
| **Net wall-clock** | | **~13-18 days** |

Disk estimate: ~3-4 GB across the 1278 ticker-months. Already
covered by the 50-200 GB free-space target.

---

## Commands

Two separate runs — Tier-1 first (smaller, faster, validates the
config), Tier-2 second. Tier-2 can resume independently if Tier-1
finishes overnight.

### Pre-flight

```bash
cd /Users/berkay/Documents/uoa-detector-
set -a; . ./.env; set +a

# Confirm Theta Terminal v3 is up on port 25503
curl -sS -o /dev/null -w "%{http_code}\n" \
  "http://127.0.0.1:25503/v3/option/list/symbols?root=SPY&format=json"
# expect: 410 (deprecated v2 param) OR 200 — anything other than
# connection error means the Terminal is alive.

# Confirm 50 GB free
df -h .
```

### Run 1 — Tier-1 anchor (~5-7 days)

```bash
PYTHONPATH=src .venv/bin/python scripts/download_tier2.py \
  --tier tier1_anchor \
  --start-date 2024-11-01 --end-date 2026-04-30 \
  --max-dte 60 \
  --concurrency 4 \
  --log-level INFO 2>&1 | tee logs/tier1_download.log
```

Resume after a crash: same command — `.download_state.json`
records progress and skips done tasks.

### Run 2 — Tier-2 starter (~8-11 days)

Wait until Run 1 finishes (or run in a separate terminal tab if
you want to overlap; Theta Terminal handles both, ThetaData Pro
quota covers both at concurrency=4).

```bash
PYTHONPATH=src .venv/bin/python scripts/download_tier2.py \
  --tier tier2_starter \
  --start-date 2024-11-01 --end-date 2026-04-30 \
  --max-dte 60 \
  --concurrency 4 \
  --log-level INFO 2>&1 | tee logs/tier2_download.log
```

### Daily monitoring (another terminal)

```bash
PYTHONPATH=src .venv/bin/python scripts/download_status.py \
  data/historical/tier1_anchor data/historical/tier2_starter
```

Output shows per-tier `done / pending / in_progress / failed`,
accumulated rows, throughput in ticker-months/hour, ETA. If
`throughput < 1 ticker-month/hour` is reported for **4+
consecutive hours**, apply the Phase 3.5 §"open question" rule:

  1. Kill both runs (`Ctrl+C` — state is preserved).
  2. Contact ThetaData support to verify Pro plan bandwidth.
  3. If throttling is confirmed → reduce universe to top-25 by
     liquidity (a new tier2_high_priority CSV; agent can build
     it on request).
  4. If not throttling → continue.

### Closeout (after both runs finish)

```bash
# Re-build manifests (validates schema + counts gaps)
PYTHONPATH=src .venv/bin/python scripts/download_tier2.py \
  --tier tier1_anchor --validate-only
PYTHONPATH=src .venv/bin/python scripts/download_tier2.py \
  --tier tier2_starter --validate-only

# Commit manifests (parquet data stays gitignored)
git add data/historical/tier1_anchor/.manifest.json \
        data/historical/tier1_anchor/.download_state.json \
        data/historical/tier2_starter/.manifest.json \
        data/historical/tier2_starter/.download_state.json
git commit -m "Phase 3.5.3: 18mo Tier-1 + Tier-2 download closeout"
git push origin phase-3
```

Then ping the agent: Phase 3.5.5 (real-data 4-cell backtest)
becomes unblocked.

---

## Resume / failure semantics

| Situation | What to do |
|---|---|
| Process crashed mid-task | Re-run same command; orchestrator skips done tasks, retries in_progress as pending. |
| Single ticker-month keeps failing | State file's `error` field has the contract-level error; if 472 / network, retry; if a Pydantic ValidationError, surface to agent (Phase 3.3.7-3.3.13 may have missed an edge). |
| Network drop overnight | Resume picks up clean. No data corruption (parquet writes are atomic per contract). |
| Laptop closed mid-run | Same — resume picks up. The TTL caches in providers warm up cold. |
| Run accidentally killed before manifest re-write | `--validate-only` rebuilds it from the on-disk parquet files. |

---

## Alternative configurations (NOT default, but available)

These cut wall-clock further at the cost of statistical guarantees
in Phase 3.5.6. Each has a falsification implication:

| Config | Wall-clock | Sample-size impact |
|---|---|---|
| `--start-date 2025-05-01` (12 months) | ~9-13 days | Tier-1 best cell ~30 trade (Phase 3.5.6 sample-size hard rule kicks in). Walk-forward quarter shrinks to 3 months. |
| Tier-1 only (skip Run 2) | ~5-7 days | Phase 3.5.6 (tier-2, *) cells return INSUFFICIENT — `(tier1, fusion) > (tier2, fusion)` falsification cannot be tested. |
| `--max-dte 30` (instead of 60) | ~3-5 days saved | Drops Track B's 31-60 DTE backtest coverage. v5_gamma_squeeze profile still penalises 30-60 DTE to 0.30; cutting this band off is a profile-tuning question, not a download question. **Do NOT use during validation** (Phase 3.5 D4 forbids tuning during validation). |

Default is **18 months × full DTE-60 filter × both universes** —
balances wall-clock against verdict confidence.
