# Backtest framework — storage, replay, metrics, and 4-cell runner

This document is the operator's tour of the backtest framework. It
covers persistence (Phase 3.2.1, in-memory + SQLite store), historical
replay (Phase 3.2.2, parquet harness), the metric calculator
(Phase 3.2.3, success criteria + pass/fail thresholds + `backtest
report` CLI), and the walk-forward orchestrator + 4-cell
combinatorial runner (Phase 3.2.4, `backtest run-4cell` CLI +
markdown comparison report with the Formülasyon A falsification
section). With Phase 3.2.4 complete, Phase 3.3's first real
backtest can begin.

## What the store is for

Every event the orchestrator processes ends in a labeled, sized signal.
Phase 1-2 dropped these into a Python list (`BacktestStore`) and
discarded them when the process exited. That works for unit tests; it
does not work for a real backtest, where:

  - A 2-year run over Tier-2 universe produces hundreds of thousands
    of decision records that don't fit in RAM
  - The metric calculator (3.2.3) needs to read the same dataset later
    in a different process
  - Phase 3.3.x cross-cell comparison ("cell 4 beat cell 1") needs to
    prove both cells ran on the same data with the same profile and no
    silently-skipped events
  - Latency analysis needs to flag pipelines too slow to act on live

The store layer addresses these. Two implementations satisfy a single
`BacktestStoreProtocol`:

  - `BacktestStore` (in-memory) — Phase 1-2 surface preserved; useful
    for tests and quick smoke runs. Loses data on process exit.
  - `SqliteBacktestStore` (persistent) — Phase 3.2.1; SQLite + WAL +
    Alembic-managed schema. Default for any non-trivial run.

Both expose the same Protocol so test code, the orchestrator, and the
metric calculator can swap them transparently.

## Schema (v1)

Four tables. Tables are created by Alembic on first open; subsequent
opens upgrade to head (no-op when already current).

### `backtest_run`

One row per backtest invocation. The audit trail Phase 3.3.x reads to
gate cross-cell comparisons.

| column | purpose |
|---|---|
| `run_id` | Primary key. uuid4 hex by default; can be set explicitly (4-cell runner uses `cell_2_t1_fusion` etc). |
| `started_at` / `finished_at` | UTC timestamps; `finished_at` is set by `finish_run()`. |
| `profile_id` / `profile_content_hash` | Calibration profile identity at run time. Cross-cell comparison requires identical hash. |
| `universe_id` | `tier1_anchor`, `tier2_starter`, or whatever the runner labels its universe with. Free-form string. |
| `source_config_hash` | Hash of which sources were active and their config (Polygon/UW/IBKR enable flags, fusion threshold). Cross-cell needs identical. |
| `total_signals_processed` | Bumped by the store on each `add()`. |
| `total_errors` | Bumped on each `record_error()`. The "ran cleanly" precondition for Phase 3.3.x is `total_errors == 0`. |
| `dataset_window_start` / `dataset_window_end` | The replay range. Cross-cell requires identical window. |
| `notes` | Free-form. |

### `signal`

One row per processed event. The decision-record stream the metric
calculator reads.

| column | purpose |
|---|---|
| `(run_id, event_id)` | Composite primary key. Same event can appear in multiple runs (the 4 cells of the matrix all replay the same events). |
| `ticker` / `ts` / `label` / `max_r` | Cross-cell comparison axes. |
| `combined_score_pre` / `combined_score_post` | Pre- and post-penalty scores. |
| `profile_id` / `profile_content_hash` | Redundant with the run row, intentionally — the metric calculator does not have to JOIN to verify "every signal under this profile". |
| `pipeline_latency_ms` | End-to-end stage chain wall-clock. The 3.2.3 metric calculator can flag a pipeline so slow you would never act on its signals live. |
| `data_source_latency_ms` | Source emission → orchestrator entry. How stale was the data when we acted on it? |
| `full_record_json` | Complete `StoredSignal` Pydantic dump. Top-level columns are query axes; the JSON blob is the audit record for any post-hoc analysis. |

Indexes: `(run_id, ts)`, `(run_id, ticker)` — the metric calculator's
expected access patterns.

### `backtest_run_error`

One row per stage error encountered during a run. Phase 1-2 errors
were logged-and-lost; from 3.2.1 onward they are persistent.

| column | purpose |
|---|---|
| `error_id` | Synthetic auto-increment PK. |
| `run_id` | FK to the parent run. |
| `event_id` | Nullable — some errors happen before an event_id is assigned (source init, data fetch). |
| `stage_name` / `error_type` / `error_message` / `occurred_at` | Self-explanatory. |

Index: `(run_id, occurred_at)` — error timeline per run.

### `alembic_version`

Single-row table managed by Alembic itself. Holds the current schema
revision string (`0001_initial` at v1). The store's `schema_version`
property derives an integer from this for callers that prefer a
numeric comparison.

## Run lifecycle: permissive default, strict opt-in

By default, the store auto-creates an "implicit" run on the first
`add()` call so Phase 1-2 callers (and the current `python -m
uoa_detector run` smoke command) keep working without changes:

```python
store = BacktestStore()
store.add(event, decision, size)  # auto-creates run_id="implicit-default"
```

This is the right default for tests, smoke runs, and exploratory work.
It is the wrong default for the Phase 3.2.4 4-cell runner, which needs
to assert each cell has its own `run_id` with no leakage from a
shared implicit run. For that case, opt into strict mode:

```python
store = SqliteBacktestStore(url, strict_run_lifecycle=True)
store.start_run(profile, run_id="cell_1_tier1_single", universe_id="tier1_anchor")
store.add(event, decision, size)
store.finish_run()
store.start_run(profile, run_id="cell_2_tier1_fusion", ...)
# ...
```

In strict mode, calling `add()` or `record_error()` without a preceding
`start_run()` raises `RunLifecycleError`. The pattern follows Phase 2's
`sub_score_missing_behavior`: permissive default for the common case,
strict opt-in for the machinery that needs the guarantees.

## `finish_run()` vs `close()` — two different lifecycles

Two distinct boundaries, two distinct methods:

| | `finish_run()` | `close()` |
|---|---|---|
| ends | the **active run** | the **whole store** |
| writes | sets `finished_at`, flushes batched buffers | flushes, finalises any active run, disposes the engine |
| after | store is **still usable**; can `start_run()` again | every public method raises `RuntimeError` |
| idempotent? | in permissive mode yes (no-op when no active run) | always (second `close()` is a no-op) |

The two boundaries exist because Phase 3.2.4's 4-cell runner reuses
**one** store across **four** runs (one per cell, distinct `run_id`s).
Closing the store between cells would force the runner into "one
SQLite file per cell, then merge" — explicitly rejected by the
acceptance doc. Instead the runner pattern is:

```python
store = SqliteBacktestStore(url, strict_run_lifecycle=True)
try:
    for cell_name in CELLS:
        store.start_run(profile, run_id=cell_name, universe_id=...)
        await pipeline.run()        # pipeline.run() calls store.finish_run()
        # store is still alive here — next iteration can start_run() again
finally:
    store.close()                   # one trailing close, after all cells
```

`Pipeline.run()` in this scheme calls `store.finish_run()` in its
`finally` block (closing the active run when the stream drains or
errors), but it does **not** call `store.close()` — the orchestrator
does not own the store's lifecycle. The CLI does. The 4-cell runner
will. Tests verify this contract: `test_finish_run_does_not_close_store`,
`test_pipeline_run_does_not_close_store`, `test_close_makes_store_unusable`.

## CLI

The `--store` flag selects the backend:

```sh
# Default — in-memory, lost on exit
python -m uoa_detector run

# Persistent SQLite — data survives the process
python -m uoa_detector run --store sqlite:./backtest.db

# Persistent SQLite at an absolute path
python -m uoa_detector run --store sqlite:/tmp/exp42.db
```

The `:memory:` literal is the explicit form of the default. The
`sqlite:` URL accepts both relative and absolute paths; SQLAlchemy
canonicalises internally.

## Replay harness — historical data into the same pipeline

Phase 3.2.2 added `ParquetReplaySource`: a `RawFlowSource` Protocol
implementation that reads historical parquet files and emits them
through the same fusion + orchestrator path the live sources use.
The orchestrator cannot tell synthetic from historical from live
data at the boundary — it sees `RawPrint` events either way, and
fusion does the source-arbitration math on event timestamps, not
walltimes.

### File format and directory convention

```
data/historical/{source}/{ticker}/{YYYY-MM}.parquet
```

One file per (source, ticker, calendar month). Months are inclusive,
ascending; gaps are tolerated (warn-logged, not fatal). Within a
file, rows must be event-time-monotonic by `timestamp` ascending —
the reader validates this row-by-row using a streaming PyArrow
`RecordBatch` reader (no whole-file load) and raises
`DataIntegrityError` on the first out-of-order row.

The parquet schema is exactly:

| column | type | nullable | source |
|---|---|---|---|
| source_id | string | no | RawPrint |
| source_event_id | string | no | RawPrint |
| timestamp | timestamp[ns, UTC] | no | RawPrint |
| ticker | string | no | RawPrint |
| option_type | string | no | RawPrint |
| strike | decimal128(20, 6) | no | RawPrint |
| expiry | date32 | no | RawPrint |
| dte | int32 | no | RawPrint |
| spot_price | decimal128(20, 6) | no | RawPrint |
| premium_paid | decimal128(20, 6) | no | RawPrint |
| option_price | decimal128(20, 6) | no | RawPrint |
| bid | decimal128(20, 6) | no | RawPrint |
| ask | decimal128(20, 6) | no | RawPrint |
| fill_side | string | no | RawPrint |
| exchange | string | no | RawPrint |
| implied_volatility | float64 | yes | RawPrint |
| open_interest | int32 | yes | RawPrint |
| is_iso | bool | no | RawPrint |
| source_tags | list&lt;string&gt; | no | RawPrint |
| arrival_ts | timestamp[ns, UTC] | yes | replay metadata |
| replay_ts | timestamp[ns, UTC] | yes | replay metadata (always NULL on disk) |

The schema is pinned by `RAWPRINT_PARQUET_SCHEMA` in
`uoa_detector.backtest.parquet_schema`. Strict-match validation runs
on every file open: if the on-disk schema gains, loses, or retypes
any field, `ParquetSchemaMismatchError` raises with a "regenerate"
hint pointing the operator at the snapshot exporter (Phase 3.5+) or
the test fixture generator (now).

### Time fields — three of them

| field | meaning | populated by |
|---|---|---|
| `timestamp` | exchange print time (when the trade actually happened) | source feed |
| `arrival_ts` | walltime the live system received this print | snapshot exporter (Phase 3.5+) — historical dumps may leave NULL |
| `replay_ts` | walltime the replay harness emitted this row in the current run | always NULL on disk; harness fills at emission time |

The triple is the audit trail for replay determinism debugging: if
two backtest runs over the same dataset window produce different
signal counts, diff their `replay_ts` distributions to localise the
non-determinism (file order, k-way merge, async scheduling).

### Reading: file discovery, k-way merge, ordering

`ParquetReplaySource` is a single-source reader. Construction
snapshots the file list at first read; files dropped into `data_dir`
mid-replay are NOT picked up (determinism precondition). Within a
single (source, ticker), all month files merge via `heapq.merge` on
event timestamp into one ascending stream; only one row per file
buffered in the heap at any time. Multi-source replay (Polygon + UW
+ IBKR all replaying at once) wires three `ParquetReplaySource`
instances into the same `SourceFusion` — fusion does the per-source
watermarking, no new fusion code.

### Pacing and replay correctness

`replay_speed=inf` (default) emits as fast as possible; positive
finite values pace the emission. SourceFusion's watermark is
**event-time** (Phase 2.3.3 invariant, originally pinned by
`test_window_differentiation_dict_equality` in
`tests/unit/test_source_fusion.py`), so correctness is independent
of `replay_speed`. The pin for the harness layer is
`test_replay_at_inf_preserves_fusion_correctness` in
`tests/unit/test_replay_fusion.py` — it runs the same sequence at
`replay_speed=inf` and `replay_speed=100` and asserts the fusion
output is byte-identical on `(confidence_tier, sources_seen)`.

### Edge cases

| condition | behaviour |
|---|---|
| empty parquet (header only, 0 rows) | warn-log + skip; not fatal |
| missing month in a contiguous range | warn-log gap + skip; not fatal |
| corrupt parquet (un-openable) | re-raise as `DataIntegrityError` |
| schema mismatch (missing/extra/retyped field) | `ParquetSchemaMismatchError` with regenerate hint |
| out-of-order row mid-file | `DataIntegrityError` on the first violating row, no whole-file load |
| file added after replay start | NOT picked up; snapshot is at first read |

Each is pinned by an explicit named test under
`tests/unit/test_parquet_replay.py`.

### CLI

```sh
# Single-source historical replay → SQLite store
python -m uoa_detector run \
    --source historical \
    --data-dir data/historical/synthetic \
    --store sqlite:./run.db \
    --output json

# With universe + date filters
python -m uoa_detector run \
    --source historical \
    --data-dir data/historical/polygon \
    --tickers AAPL,MSFT,NVDA \
    --from 2024-01 \
    --to 2024-12 \
    --store sqlite:./2024.db \
    --output json

# Real-time pacing (rare; for live-equivalent debugging)
python -m uoa_detector run \
    --source historical \
    --data-dir data/historical/synthetic \
    --replay-speed 1.0
```

Multi-source replay (three sources at once) is not exposed via the
CLI in 3.2.2; the Phase 3.2.4 4-cell runner will be the first
explicit consumer of multi-source replay.

### How to provide your own historical data

Two paths today:

1. **From in-memory `RawPrint`s** — useful for tests, fixture
   regeneration, or one-off conversions:

   ```python
   from pathlib import Path
   from uoa_detector.backtest.parquet_schema import write_parquet

   prints = [...]  # list[RawPrint], must be timestamp-monotonic
   write_parquet(prints, Path("data/historical/mysource/AAPL/2025-06.parquet"))
   ```

2. **From a snapshot exporter** — Phase 3.5+, not landed yet. The
   snapshot exporter will tap a live source's incoming feed and
   write parquet on a rolling basis with `arrival_ts` populated.

The synthetic fixture
`tests/fixtures/historical/synthetic/AAPL/2025-06.parquet` is the
worked example: 10 hand-crafted RawPrints across 5 trading days,
generated by `make_synthetic_aapl_2025_06_fixture()` in the same
schema module. Any new source layout that respects the directory
convention and the schema pin will replay through the same harness
without code changes.

## Metric calculator — pinning what "did the edge work" means

Phase 3.2.3 added a metric calculator that converts a stream of
trade outcomes into the five Track B + Formülasyon A success
metrics approved in Phase 3 prep step 3. The math is fixed here —
subsequent commits and Phase 3.3's first real backtest cannot
quietly redefine the answer.

### Formulas

| metric | formula | None when |
|---|---|---|
| **Sharpe** (annualized) | `(mean / stdev_sample) * sqrt(252)` over daily realized-R series | `total_trades < 30` OR `stdev = 0` |
| **Expectancy E** | arithmetic mean of `realized_r` over closed trades | always defined; 0 on empty |
| **Walk-forward consistency** | trades sorted by `exit_ts`, split into N equal-trade-count chunks; consistency = `count(E_i > 0) / N` | `total_trades < N` |
| **Max drawdown** | peak-to-trough on cumulative-R, denom = `max(running_peak, 1)` | always defined; 0 on empty |
| **Total trades** | count of `realized_r is not None` | always defined |

Plus three diagnostics: hit rate, avg winner R, avg loser R.

The daily realized-R series is built by summing `realized_r` per UTC
date of `exit_ts`. Two trades exiting on the same day collapse into
a single daily return — without this, 60 same-day trades would
inflate `len(daily)` and shift Sharpe by `sqrt(60)`.

### Default thresholds (Track B + Formülasyon A)

| threshold | default | meaning |
|---|---|---|
| `sharpe_pass` | 1.5 | Sharpe > this is a pass |
| `sharpe_bonus` | 2.5 | Sharpe > this is a separately-flagged "bonus" |
| `expectancy_pass` | 0.5 | E > this (R units) is a pass |
| `walk_forward_consistency_pass` | 0.75 | fraction of windows with E > 0 |
| `max_drawdown_ceiling` | 0.20 | max DD < 20% is a pass |
| `min_trades` | 60 | total trades >= this for the run to be meaningful |

These defaults are pinned by
`test_track_b_thresholds_match_phase_3_prep`. Override via the
`MetricThresholds` constructor is supported; the defaults cannot
drift without breaking that test.

### Pass/fail semantics

Each metric returns one of `pass`, `fail`, `insufficient_sample`.
The third state surfaces when the underlying value is `None` (e.g.
Sharpe with < 30 trades, walk-forward with < N trades). A run with
many `insufficient_sample` outcomes is a signal that the dataset is
too small to evaluate the strategy, distinct from a strategy that
trades enough but loses money. The 4-cell runner (Phase 3.2.4) uses
this distinction when comparing cells.

`overall_pass` is `True` only when every metric is `pass` — a single
`fail` or `insufficient_sample` blocks the run from passing overall.

### PnL boundary — three providers ship in 3.2.3

The metric calculator does NOT compute realized R itself. It receives
a `PnLProvider` Protocol implementation that maps decision records to
`RealizedTrade`s. Phase 3.2.3 ships three:

| provider | use case | exits |
|---|---|---|
| `NoOpPnLProvider` | wiring smoke; CLI report default | always open |
| `MockPnLProvider` | unit tests with fixture R values per `event_id` | per fixture |
| `SimplePnLProvider` | basic-but-consistent realistic pricing | entry=ask, exit=bid via ExitQuoteProvider, slippage haircut |

`SimplePnLProvider` is the model approved for Phase 3.3's first real
backtest. Pricing details:

  - **Entry**: option ask at the decision's timestamp (the
    `option_price` field on the StoredSignal).
  - **Exit**: option bid at the holding-window close, looked up via
    the injected `ExitQuoteProvider`.
  - **Slippage**: `profile.backtest.slippage_pct` of entry premium,
    haircut on per-contract PnL.
  - **R-units**: 1R ≡ entry premium. With `max_r=0.5`, realized_r is
    half magnitude.

Holding strategy comes from `profile.backtest.holding_strategy`:

  - `fixed_window`: close at `entry + holding_window_days`, capped
    by the `exit_on_dte_lte` floor.
  - `dte_based`: close at `expiry - dte_based_close_threshold`, also
    capped by floor.
  - `take_profit_or_stop`: reserved enum value; `SimplePnLProvider`
    raises `NotImplementedError("... Phase 3.4 ...")`. Profiles that
    select this strategy in 3.2.3 fail at first call rather than
    silently degrade.

Open trades (no quote available, max_r=0, degenerate entry price,
or holding window not yet closed) round-trip as `realized_r=None`
and are excluded from every numerical metric. They DO surface as
`open_trades` in the metrics output so reports can flag runs where
a large fraction of decisions never closed.

### CLI: `backtest report --run-id <X>`

```sh
# Plumbing-only report — every signal counts as an open trade
python -m uoa_detector backtest report \
    --run-id implicit-default \
    --store sqlite:./run.db

# Custom walk-forward partition (default 8)
python -m uoa_detector backtest report \
    --run-id implicit-default \
    --store sqlite:./run.db \
    --walk-forward-windows 4
```

The default `--pnl noop` reports every signal as an open trade —
useful for confirming wiring (did decisions reach the store?) and
diagnosing replay determinism (is the run_id correct?). Real
quote-driven reporting (`--pnl simple`) needs an exit-quote source
wired from the replay stream; that lands in Phase 3.3, and 3.2.3
returns a `--pnl simple` invocation with a clear deferral message.

Sample output (NoOp on a 10-event historical replay):

```
=== Backtest report — run_id=implicit-default ===
Total trades:       0
Open trades:        10
Hit rate:           0.000
Expectancy E:       +0.0000 R
Sharpe (annual):    n/a (insufficient sample)
Walk-forward:       n/a (insufficient sample for window count)
Max drawdown:       0.000

--- Pass/fail vs thresholds ---
  sharpe                       value=n/a        threshold=1.5000     INSUFFICIENT_SAMPLE
  expectancy                   value=0.0000     threshold=0.5000     INSUFFICIENT_SAMPLE
  walk_forward_consistency     value=n/a        threshold=0.7500     INSUFFICIENT_SAMPLE
  max_drawdown                 value=0.0000     threshold=0.2000     PASS
  total_trades                 value=0.0000     threshold=60.0000    FAIL

OVERALL: FAIL
```

## Walk-forward methodology + 4-cell combinatorial backtest

Phase 3.2.4 adds the capstone of the 3.2.x backtest framework: a
single CLI command that runs the **Formülasyon A 4-cell
combinatorial backtest** end-to-end, with **walk-forward
windowing** providing per-window expectancy values for the
walk-forward consistency metric.

### Walk-forward windowing (3.2.4.1)

The full backtest period is split into `N` equal-time slices
(default `N=8` over 2 years = 3-month slices, tunable via
`--walk-forward-windows` and `profile.backtest.walk_forward_windows`).

  - **Boundary convention**: `[start_i, start_{i+1})` half-open;
    the very last window is closed-closed so the period's final
    instant isn't lost. Pinned by
    `test_window_boundaries_half_open_exhaustive`: sweep 100
    timestamps and assert each lives in exactly one window.
  - **Last window absorbs rounding remainder** so the union of
    windows exactly covers `[period_start, period_end]`.
  - **In-sample / out-of-sample split** is symbolic in 3.2.4 —
    `in_sample_fraction=1.0` means "data seen, profile NOT tuned"
    (the same frozen profile runs everywhere). The
    `WalkForwardWindow.in_sample_end` property already computes
    the boundary so Phase 3.4+ can drop in real auto-tuning
    without changing the windowing API.
  - **Tuner**: a `Callable[[CalibrationProfile, WalkForwardWindow],
    CalibrationProfile]`. Phase 3.2.4 ships `no_op_tuner` (returns
    input unchanged) as the only implementation.

The walk-forward consistency metric (Phase 3.2.3) reads
slice-level expectancies and rolls them up as a **fraction of
windows positive** (not an integer count); the pass threshold is
`profile.backtest.walk_forward_min_consistency_pct = 0.75`. This
maps to "6 of 8" at default `N=8` and survives a future change to
`N=4` (3 of 4) or `N=16` (12 of 16) without re-tuning. The
fraction-not-count representation is the explicit reason the
threshold doesn't drift across window-count changes.

### The 4-cell matrix (3.2.4.2)

The Formülasyon A hypothesis is **combinatorial**: a real edge
requires both Tier-2 universe AND multi-source fusion. The 4-cell
backtest is the experiment designed to falsify that hypothesis.

|              | single source | unanimous fusion |
|--------------|---------------|------------------|
| **Tier-1**   | `tier1_single` (baseline) | `tier1_fusion`   |
| **Tier-2**   | `tier2_single`            | `tier2_fusion` (the candidate winner) |

Each cell becomes a separate `run_id` in the SQLite store, with
`RunMetadata.universe_id` set to the cell name. The cell runner
calls `start_run` explicitly per cell — 3.2.4 is the first
explicit-`start_run` consumer in the codebase (Phase 1-2 + 3.2.1-3
all used the implicit-default permissive path).

  - **Tier-1 anchor universe** (`data/universes/tier1_anchor.csv`):
    20 broadly-watched, deep-options-volume tickers across 8
    sectors. Curated, no "flag for review" notes. The exact list
    is approved Phase 3 prep:
    SPY/QQQ/IWM/DIA broad ETFs;
    NVDA/AMD/AAPL/MSFT/GOOGL/TSLA/META/AMZN mega-cap tech;
    JPM/BAC financials; XOM/CVX energy;
    JNJ/UNH healthcare; GLD/TLT macro hedges.
  - **Tier-2 starter universe** (`data/universes/tier2_starter.csv`):
    the broader edge-watch list shipped in Phase 3.2.0a.

Both universes load through the same `load_universe_tickers()`
code path, asserted by `test_both_universes_load_through_same_loader`.

### Comparison report (3.2.4.3)

The 4-cell run produces a markdown report (path passed via
`--report-path`) with four sections:

1. **4×6 main metrics table** — total trades, Sharpe (with bonus
   star ⭐ if `> 2.5`), expectancy E, walk-forward, max DD,
   overall PASS/FAIL. Each non-baseline cell has `(Δ X.XXX ↑/↓)`
   raw deltas vs the `tier1_single` baseline. (Max DD uses
   "lower is better" arrow semantics.)
2. **Per-metric pass/fail breakdown** — 4 cells × 5 metrics with
   ✅ PASS / ❌ FAIL / ⚠️ INSUFFICIENT.
3. **Per-cell run metadata** — `run_id`, open trades, hit rate,
   bonus Sharpe flag.
4. **What would falsify Formülasyon A** — the heart of the
   report (see next subsection).

Deltas are **raw differences**. No statistical-significance test
is applied at this stage — Phase 3.3 will pick the
multiple-comparisons-correction approach (Bonferroni vs
Benjamini-Hochberg vs nested cross-validation). Premature
significance tests would lock us into the wrong correction.

### What would make Phase 3 conclude the edge is real

Formülasyon A is the combinatorial hypothesis: **a real edge
requires `(Tier-2, fusion)` to materially beat the other three
cells**. Each of the following partial outcomes **rejects** the
combinatorial hypothesis even if individual signals look
interesting:

  - **Only `(Tier-2, single)` beats baseline.** The fusion layer
    isn't pulling its weight; whatever Tier-2 signal exists works
    at single-source. The "fusion is necessary" hypothesis fails.
  - **Only `(Tier-1, fusion)` beats baseline.** The Tier-2
    universe isn't where the edge lives; fusion alone on the
    anchor universe is enough. The "Tier-2 is necessary"
    hypothesis fails.
  - **Both single-source cells beat both fusion cells.** Fusion
    is a net negative — it's filtering out signal, not noise.
    Combinatorial hypothesis is decisively rejected.
  - **Tier-2 fusion does NOT materially beat Tier-1 fusion AND
    Tier-2 single.** The combinatorial gain doesn't exist; the
    edge can be explained by the marginal contribution of
    fusion or universe alone, not their interaction.

A strict "Tier-2 fusion materially beats every other cell"
outcome with all five Track B thresholds passing on `tier2_fusion`
is what would let us proceed to Phase 3.3's first real backtest
with confidence in the Track B + Formülasyon A framing. **Anything
weaker is a signal to revise the hypothesis before risking
capital.**

### Determinism

The acceptance doc requires: "run the 4-cell suite twice with the
same inputs, assert the SQLite content_hash of decision records
is identical across runs." The implementation pin is stronger —
the **markdown report itself** is byte-identical across runs,
asserted by `test_run_4cell_is_deterministic_byte_identical_reports`
in `tests/integration/test_cli_run_4cell.py`. If anything in the
rendering or metric pipeline introduces non-determinism (timestamps,
dict ordering, etc.), this test fails immediately.

The `--seed` flag is wired through `run_4cell_backtest` for Phase
3.4+ tuning (which will introduce randomness from grid jitter or
Bayesian optimisation). 3.2.x is fully deterministic so the seed
currently has no effect; the flag is a no-op placeholder.

### CLI

```sh
# Full 4-cell backtest with markdown report writeout
python -m uoa_detector backtest run-4cell \
    --store sqlite:./4cell.db \
    --report-path ./reports/exp1.md \
    --from 2024-01-01 --to 2026-01-01

# Custom walk-forward partition + custom profile
python -m uoa_detector backtest run-4cell \
    --store sqlite:./4cell.db \
    --report-path ./reports/exp2.md \
    --from 2024-01-01 --to 2026-01-01 \
    --walk-forward-windows 16 \
    --profile profiles/v5_gamma_squeeze.yaml \
    --seed 42

# Per-run summary report on a single cell of an existing 4-cell run
python -m uoa_detector backtest report \
    --run-id 4cell-tier2_fusion \
    --store sqlite:./4cell.db
```

Phase 3.2.4 only ships `--trades noop` — every cell reports zero
trades because real trade producers need an exit-quote source
from the replay stream (Phase 3.3). The plumbing — `start_run`,
`finish_run`, `RunMetadata`, comparison report rendering — is
exercised end-to-end. When Phase 3.3 lands the first real trade
producer, the same `run-4cell` CLI command will produce real
metrics with no further CLI changes.

## Why WAL mode

`PRAGMA journal_mode=WAL` is set on every connection (a SQLAlchemy
event listener fires on `connect`). WAL allows concurrent reads
alongside writes — under SQLite's default rollback journal, the
metric calculator would block waiting for the orchestrator to
release its writer lock. With WAL, the metric calculator can stream
records out of a run while the next run is being written.

`PRAGMA foreign_keys=ON` is set on the same listener so that
`signal.run_id` references to `backtest_run.run_id` are enforced. It's
not on by default in SQLite (legacy compatibility) and a Phase 3.3+
mistake that orphaned signal rows would otherwise be invisible until
a JOIN noticed the dangling reference.

## Migrations: why Alembic from day 1

The schema starts at v1 and is unlikely to stay there. Phase 3.2.3
adds the metric calculator's holding-strategy / slippage / DTE-exit
columns to a new `backtest_config` table; Phase 3.4 adds real PnL
columns to `signal`. Each of these is a migration.

The two viable approaches were (1) a hand-rolled `schema_version` int
column with bespoke `if version < N: ALTER ...` dispatch, or (2)
Alembic. The first is fine until the third migration, at which point
nobody can remember what migration 2 looked like. Alembic gives
generated migration scripts, autogenerate-from-models, and downgrade
support for free. Adding the dependency now costs ~10MB of dependency
weight and saves the inevitable future rewrite.

`SqliteBacktestStore.__init__` calls `command.upgrade(cfg, "head")`
on every open. This is a no-op on already-current databases and runs
the missing migrations on stale ones. There is no command-line use:
the store is the only thing that runs migrations.

## Append-only at the public surface

The store has no `update_signal`, `update_error`, `delete_signal`, or
`delete_run` methods. The only field-level mutations are:

  - `RunMetadata.finished_at` set by `finish_run()`
  - `RunMetadata.total_signals_processed` and `total_errors`
    incremented as rows arrive

This is enforced by a test (`test_sqlite_no_public_update_method`)
that scans the public attributes and asserts none of those names
exist. Phase 3.3.x audit needs this — a backtest that changed its
historical data after the fact is not a backtest.

## What these commits do NOT do

After Phase 3.2.4 the backtest framework's surface is: store +
replay harness + PnL boundary + metric calculator + 4-cell
combinatorial runner + walk-forward windowing + CLI (`backtest
run-4cell` + `backtest report`). Currently missing — by design — are:

  - **No real trade producer.** The 4-cell runner in 3.2.4 takes a
    `TradeProducer` callable; Phase 3.2.4 ships `noop_trade_producer`
    (every cell reports 0 trades) and `fixture_trade_producer` (test
    helper). A real producer that drives the full pipeline through
    fusion + orchestrator and writes signals to the store is Phase
    3.3 territory — that's the "first real backtest" milestone.
  - **No exit-quote source from the replay stream.** Both
    `SimplePnLProvider` and the eventual real trade producer need
    `ExitQuoteProvider` instances backed by the parquet replay
    data, not test-fixture dicts. Phase 3.3.
  - **No latency measurement in the orchestrator yet.** The columns
    exist (`pipeline_latency_ms`, `data_source_latency_ms`); wiring
    happens when the first real backtest report needs the value
    (Phase 3.3).
  - **`replay_ts` not threaded through to SQLite.** Reserved in the
    parquet schema as nullable harness-written; the SQLite signal
    row's `full_record_json` does not currently carry it. Lazy
    enhancement for when cross-run determinism diffs become a real
    debugging need.
  - **No multi-source replay via the CLI.** `--source historical`
    drives one `ParquetReplaySource` against one `data_dir`. The
    4-cell runner could in principle instantiate multiple harnesses
    explicitly per cell × source combination; today it doesn't —
    Phase 3.3's real trade producer is the right place to wire that
    because the producer owns the source-fusion topology decision.
  - **No snapshot exporter.** Historical parquet files have to be
    produced by hand or by a Phase 3.5+ exporter that taps a live
    source's incoming feed.
  - **`take_profit_or_stop` holding strategy raises NotImplementedError.**
    The enum value is reserved in `BacktestConfig`. Implementation
    is Phase 3.4 (real intra-window stop/TP logic with Black-Scholes
    or full surface evolution).
  - **No statistical-significance test in the comparison report.**
    Deltas are raw differences. Phase 3.3 will pick the
    multiple-comparisons-correction approach (Bonferroni vs
    Benjamini-Hochberg vs nested cross-validation); premature
    significance tests would lock 3.2.4 into the wrong correction.
  - **No real auto-tuner.** The walk-forward windowing exposes a
    Tuner Callable; 3.2.4 ships `no_op_tuner` (returns input
    profile unchanged). In-sample auto-tuning is Phase 3.4+
    (grid search, Bayesian optimisation, etc.).
  - **No backup / archive utility.** SQLite files are just files;
    `cp` is the backup command for now.

When Phase 3.3 lands the real trade producer, the existing CLI
command `python -m uoa_detector backtest run-4cell ...` will
produce real metrics with no flag changes — the `--trades` flag
will gain `simple` (and eventually `real`) values, but the
orchestration, windowing, comparison report, and falsification
section all stay the same.

## Reading from the database

The store's `iter_records(run_id)` and `iter_errors(run_id)` are the
intended interfaces. For ad-hoc inspection, plain SQLite works:

```sh
sqlite3 backtest.db
sqlite> .tables
alembic_version  backtest_run  backtest_run_error  signal
sqlite> SELECT run_id, ticker, label, max_r FROM signal LIMIT 5;
```

The `full_record_json` column on `signal` holds the complete
`StoredSignal`; pipe it through `jq` for readable inspection:

```sh
sqlite3 backtest.db "SELECT full_record_json FROM signal LIMIT 1" | jq .
```
