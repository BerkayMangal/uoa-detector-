# Phase 3.8 — Live multi-source enrichment for the screener (acceptance contract)

Status: **FROZEN** (per D1) once code lands against it.
Owner: Berkay. Author: Claude (Opus 4.8).

---

## 1. Objective

Make the daily `screener` actually use the multi-source enrichment engine
(M21–M28) it was built for. Today `screener --source rest|live` runs
`default_stage_pipeline()`, which constructs every M-stage with **no
provider** — so each falls back to its NoOp default (D7). The result: M21
dealer-gamma, M22 catalyst, M23 price-action, M24 IV, M25 sector-peer, M26
dark-pool and M27 open-interest are all dormant in the live path, the
confluence score collapses to base-flow-only, and the daily list is hollow
and mostly empty.

This phase wires the **real Unusual Whales providers** into the live path so
the screener produces the confluence-scored candidate list the system was
designed to produce. Berkay pays for Unusual Whales + ThetaData; this build
makes the screener finally use that data.

## 2. Honest framing (non-negotiable)

This is **decision-support**, not proven edge. Phase 3.5 (the real backtest)
has not returned a verdict; nothing here claims profitability. Wiring the
real providers changes only *how much context each signal carries*, not the
scoring math, not the thresholds, not the falsification framework.

- No `profiles/*.yaml` value is touched (D8). Every threshold and every
  provider timeout continues to be read from the profile by the stages
  themselves; this phase only supplies the providers.
- Ranking is still by the pipeline's existing confluence (combined) score.
- The digest keeps the same verbatim intent header from Phase 3.6
  ("Decision-support candidates — ranked by confluence score …").

## 3. UW-key-only (no ThetaData Terminal)

M23 price-confirmation was migrated to the UW price-action provider in Phase
3.3.8 (`UnusualWhalesPriceActionProvider`, `/api/stock/{ticker}/ohlc/1m`).
Every enrichment provider in the live pipeline is therefore backed by the
**Unusual Whales REST API only**. The daily screener list requires the
`UNUSUAL_WHALES_API_KEY` and **does not** require the ThetaData Terminal to
be running. ThetaData stays a Phase-4/backtest concern.

## 4. Scope

### 4.1 `build_live_stage_pipeline(client, profile)`

New builder in `src/uoa_detector/pipeline/stages/live_stages.py`, exported
from `pipeline/stages/__init__.py`. It returns the **same stage order** as
`default_stage_pipeline()`, but each M-stage is constructed with its real UW
provider, all backed by **one shared** `UnusualWhalesClient` and the
profile's `data_sources.unusual_whales` settings. The wiring mirrors exactly
how the integration smokes construct each provider
(`tests/integration/test_m2X_smoke.py`).

Providers wired (7 M-stages, 9 provider instances):

| Stage | Provider(s) | Smoke reference |
|---|---|---|
| M21 DealerGammaStage | `UnusualWhalesDealerGammaProvider` | test_m21_smoke |
| M22 EventCalendarStage | `UnusualWhalesCatalystCalendarProvider` | test_m22_smoke |
| M23 PriceConfirmationStage | `UnusualWhalesPriceActionProvider` | test_m23_smoke |
| M24 IVExhaustionStage | `UnusualWhalesIVHistoryProvider` + `UnusualWhalesCatalystCalendarProvider` | test_m24_smoke |
| M25 SectorPeerStage | `UnusualWhalesSectorMapProvider` + `UnusualWhalesPeerFlowProvider` | test_m25_smoke |
| M26 DarkPoolStage | `UnusualWhalesDarkPoolProvider` | test_m26_smoke |
| M27 OpeningClosingStage | `UnusualWhalesOpenInterestProvider` | test_m27_smoke |

Stages with no external provider stay as-is (constructed exactly as in
`default_stage_pipeline()`): `TimeOfDayStage` (M39), `DTEDecayStage` (M35),
`SweepBlockStage` (M34), `RelativePremiumStage` (M37),
`TemporalClusterStage` (M38), `ClusterDecayStage`.

M28 is **out of scope** for the live pipeline: it is not a `PipelineStage`,
it is the overnight T+1 batch validator (`M28Validator`) that runs against a
stored run, not against the live stream. It is unchanged.

The M-stages read their per-module settings (thresholds and
`provider_timeout_s`) from `ctx.profile.scoring.modules.mXX` inside
`enrich()`; the builder does not pass timeouts and does not remove the
existing `asyncio.wait_for` wrapping (D7 preserved).

### 4.2 Wiring the CLI

- `screener --source rest`: reuse the single `UnusualWhalesClient` the REST
  path already builds for the flow source; pass it to
  `build_live_stage_pipeline(client, profile)`. One client for flow **and**
  all providers.
- `screener --source live`: the flow source is a WebSocket; the enrichment
  providers are REST. Build one dedicated `UnusualWhalesClient` from
  credentials for enrichment and pass it to `build_live_stage_pipeline`,
  closing it in a `finally`. Its lifecycle is owned by `_run_live`.
- `run`, backtest, synthetic and historical paths keep
  `default_stage_pipeline()` (NoOp providers) — they are offline/deterministic
  and must not reach out to UW.

### 4.3 Screener display policy — `--top-n` / `--strict`

`screener` gets `--top-n N` (default 15) and a `--strict` flag.

- **Default (top-n mode):** rank **all** processed signals by
  `combined_score_post_penalty` desc and render the top N regardless of
  strict label. Each row still shows its LABEL and MAX_R, so a noise/rejected
  candidate is visible **and** visibly marked. This guarantees the page is
  never empty when flow exists.
- **`--strict`:** restores the Phase 3.6 actionable-only filter
  (`max_r > 0` and label not in the non-actionable set), then ranks. This is
  the pre-3.8 behavior.

**Why this is not D4 threshold-tuning.** `--top-n` is a *display policy* for
a decision-support tool: it changes which already-scored rows are shown, not
how any row is scored. It reads and writes **no** profile threshold, changes
**no** scoring code, and moves **no** falsification boundary. The strict
filter still exists behind `--strict` and is unchanged. D4 forbids retuning
the *test condition* after seeing results; showing the operator the top N of
what the unchanged pipeline produced is orthogonal to that.

### 4.4 Self-diagnostic line

After a `screener` run, emit to **stderr**:

1. `flow rows fetched=X, mapped=Y, dropped=Z` for the REST flow source, and
   when `Z > 0`, a sample of the **keys** of dropped rows (never their
   values — no data leakage). Only meaningful for `--source rest` (the source
   that fetches-then-maps in one shot); other sources report `n/a`.
2. A compact per-enrichment-module OK / no-data / error tally computed from
   the collected `SignalDecisionRecord.stage_executions[*].metadata`
   (`branch` + `provider_returned`), so one run reveals end-to-end data
   health for M21–M27.

The diagnostic reads existing telemetry; it adds no scoring and no
thresholds.

## 5. Blocked-not-hollow rule

If any provider genuinely cannot be constructed from `(client, profile)` the
way the smokes do, or wiring a real provider breaks a stage's contract, the
build **stops and reports** the specific stage + file:line. It never silently
falls back to a NoOp — hollow enrichment is the exact bug this phase fixes.

## 6. Done-when

- `build_live_stage_pipeline(client, profile)` returns the same stage order
  as `default_stage_pipeline()` with all 7 M-stages carrying their real UW
  provider classes (asserted by unit test with fake clients, no network).
- `screener --source rest` and `--source live` run that pipeline over one
  shared UW client each; `run`/backtest/synthetic/historical untouched.
- `--top-n` (default) never yields an empty page when flow exists; `--strict`
  restores actionable-only filtering. Both covered by unit tests over a fake
  source.
- Diagnostic line content covered by a test.
- Every commit green: `uv run pytest -q && uv run mypy --strict src/ &&
  uv run ruff check .`. No profile edits. New behavior gets new tests; no
  existing test weakened (D10).

## 7. The command Berkay runs

UW key only, no ThetaData Terminal:

```bash
export $(grep -v '^#' .env | xargs)          # loads UNUSUAL_WHALES_API_KEY
uv run uoa-detector screener \
  --source rest \
  --live-tickers AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,SPY,QQQ \
  --top-n 15
```

Add `--strict` to see only actionable candidates. Add `--since-minutes N` to
window the recent-flow fetch. The digest prints to stdout and a markdown twin
lands under `reports/screener_<date>.md`; the diagnostic line prints to
stderr.

---

## 8. Closeout (paket-mode)

Status: **KAPALI**. Contract met; every commit green.

### Commits

- `Phase 3.8.1` — freeze this acceptance contract.
- `Phase 3.8.2` — `build_live_stage_pipeline` wires the real UW providers
  (`pipeline/stages/live_stages.py`), exported from `stages/__init__.py`.
- `Phase 3.8.3` — `screener --source rest|live` use the live pipeline; one
  shared UW client per path; `run`/backtest/synthetic/historical untouched.
- `Phase 3.8.4` — `--top-n` (default) / `--strict` display policy + stderr
  self-diagnostic (flow ingestion + per-module OK/no-data/error tally).

### Verified clean (at HEAD)

- `uv run pytest -q` → 1531 passed, 20 skipped.
- `uv run mypy --strict src/` → Success, 116 source files.
- `uv run ruff check .` → All checks passed.

### New tests (+27 over the 1504 baseline)

- `tests/unit/test_live_stage_pipeline.py` (4): same stage order as default;
  each M-stage carries its real UW provider (not NoOp); default pipeline
  still NoOp; all providers share the one injected client.
- `tests/unit/test_digest.py` (+4): `screen_top_n` ranks all labels, never
  empty when flow exists, caps at N, empty on N<=0.
- `tests/unit/test_diagnostics.py` (8): branch classification, per-module
  tally, flow-line + enrichment-line rendering.
- `tests/unit/test_uw_rest_flow.py` (+3): fetched/mapped/dropped counters,
  zero-before-stream, windowed rows still count as mapped.
- `tests/integration/test_cli_screener_rest.py` (+2) and
  `test_cli_screener.py` (+1): diagnostic lines on stderr; top-N vs strict.

### Judgment calls

1. **New module `live_stages.py`** rather than growing `stages/__init__.py`.
   Keeps the offline `default_stage_pipeline` and the online builder visibly
   separate; the guard test pins that the default stays NoOp.
2. **`--source live` builds a second, dedicated UW client** for enrichment.
   The live flow feed is a WebSocket; enrichment is REST. One client for
   both would tangle two lifecycles; the enrichment client is closed in a
   `finally`. `--source rest` does share its single client (flow + all
   providers) as the contract requires.
3. **`screen_top_n` is a new function**, not a changed `screen_records`.
   `screen_records` stays the strict view (its Phase 3.6 tests are the
   cumulative spec, D10). One existing CLI test asserted the old default
   (actionable-only); it was updated to the new default and given a
   `--strict` counterpart — a documented D10 flag, not a silent weakening.
4. **Data-health classifier is branch-first.** `branch` is present on every
   stage; `provider_returned` is not uniform (M24 uses `iv_provider_returned`,
   M25 has none). Timeout/error branches → error; an explicit no-data branch
   set → no_data; everything else → ok; `preset_skip`/missing → not counted.
5. **`_MAX_DROPPED_SAMPLES` and `--top-n` default (15) are display/rendering
   values, not scoring thresholds** — they live in code, not the profile,
   because D8 governs numeric truth for *scoring*, and these touch neither a
   score nor the falsification framework.
6. **M28 stays out of the live pipeline.** It is the T+1 overnight batch
   validator (`M28Validator`), not a `PipelineStage`; wiring it here would be
   wrong. Unchanged.

### Honest framing

This makes the screener *use* the multi-source data — it does **not** prove
edge. Phase 3.5 (the real backtest) owns the edge verdict; nothing here
claims profitability. The digest keeps its decision-support header.

### Sıradaki

Berkay runs the §7 command with a live UW key to confirm real providers
return data end-to-end (the diagnostic line is the one-look health check).
Any dead module shows as `err=` or `nodata=` for every row and is a named,
one-line fix — not a silent hollow list.
