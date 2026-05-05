# Backtest framework — storage and replay

This document is the operator's tour of the backtest framework. It
covers persistence (Phase 3.2.1, in-memory + SQLite store) and
historical replay (Phase 3.2.2, parquet harness). The metric
calculator (3.2.3) and walk-forward + 4-cell runner (3.2.4) will
extend this doc with their own sections when they land.

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

The 3.2.x series is the backtest framework's plumbing, not its
analysis layer. Currently missing — by design — are:

  - **No latency measurement in the orchestrator yet.** The columns
    exist (`pipeline_latency_ms`, `data_source_latency_ms`), the
    round-trip is tested, but `Pipeline.process_one` does not
    currently populate them. Wiring lands when the metric calculator
    (Phase 3.2.3) needs the value.
  - **No new orchestrator integration with `start_run`.** Phase 1-2
    callers (including the CLI in both synthetic and historical
    modes) hit the implicit-default run path. The 4-cell runner in
    Phase 3.2.4 will be the first explicit `start_run` user.
  - **`replay_ts` not threaded through to SQLite.** The parquet
    schema reserves `replay_ts` as nullable harness-written; the
    harness emits `RawPrint` (which has no `replay_ts` field), so
    the SQLite signal row's `full_record_json` does not currently
    carry `replay_ts`. Threading it through is a lazy enhancement
    when cross-run determinism diffs become a real debugging need.
  - **No multi-source replay via the CLI.** `--source historical`
    drives one `ParquetReplaySource` against one `data_dir`. Phase
    3.2.4's 4-cell runner will instantiate multiple harnesses
    explicitly (one per cell × source combination) and own the
    fusion wiring directly.
  - **No snapshot exporter.** Historical parquet files have to be
    produced by hand or by a Phase 3.5+ exporter that taps a live
    source's incoming feed. For now, fixtures are generated via
    `make_synthetic_aapl_2025_06_fixture()` and `write_parquet()`.
  - **No metric calculation, no walk-forward, no cell comparison.**
    Those are 3.2.3 + 3.2.4.
  - **No backup / archive utility.** SQLite files are just files;
    `cp` is the backup command for now.

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
