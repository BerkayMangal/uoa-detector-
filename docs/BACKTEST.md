# Backtest framework — storage, replay, and metrics

This document is the operator's tour of the backtest framework. It
covers persistence (Phase 3.2.1, in-memory + SQLite store), historical
replay (Phase 3.2.2, parquet harness), and the metric calculator
(Phase 3.2.3, success criteria + pass/fail thresholds + `backtest
report` CLI). The walk-forward + 4-cell runner (3.2.4) will extend
this doc with its own section when it lands.

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

## Metric calculator — Track B + Formülasyon A success criteria

Phase 3.2.3 added the metric calculator: a stateless function that
turns a list of `RealizedTrade` into a `BacktestMetrics` containing
the five Track B + Formülasyon A success metrics plus their
pass/fail breakdown against pinned thresholds. The math is fixed
here so subsequent commits and Phase 3.3's first real backtest
cannot silently redefine "did the edge work".

### Formulas

| metric | formula | pass criterion (default) |
|---|---|---|
| **Sharpe (annualized)** | `mean(daily_R) / stdev(daily_R) × √252`; `None` if total trades < 30 or stdev = 0 | `> 1.5` (`> 2.5` flagged separately as "bonus") |
| **Expectancy E** | arithmetic mean of per-trade R | `> 0.5R` |
| **Walk-forward consistency** | trades sorted by exit_ts, partitioned into N equal-trade-count windows; consistency = fraction of windows where E > 0; `None` if fewer than N trades available | `≥ 0.75` (N-independent — survives changes to N) |
| **Max drawdown** | peak-to-trough on cumulative-R curve, as fraction of `max(running_peak, 1)` | `< 0.20` (20%) |
| **Total trades** | count of decisions with non-zero `max_r` AND non-`None` realized_r | `≥ 60` |

Plus three diagnostics that don't gate pass/fail but appear in the
report:

  * **Hit rate** = fraction of total trades with `realized_r > 0`.
    A break-even trade (R = 0) falls in the loser bucket, by
    convention.
  * **Avg winner R** = mean realized_r over winning trades only;
    `None` if zero winners.
  * **Avg loser R** = mean realized_r over non-winning trades; if
    all trades were winners, falls back to mean of all trades so the
    report has *something* to show.

### Open trades — what they are and why they're excluded

A trade is "open" when its `realized_r` is `None`. Three production
reasons this happens:

  1. The decision didn't take a position (`max_r == 0`).
  2. The holding window hasn't closed by the end of the available
     data (e.g. backtest cut off mid-trade).
  3. The exit-quote source has no quote at the computed exit time.

Open trades are counted under `BacktestMetrics.open_trades` and
**excluded from every numerical metric** — Sharpe, expectancy,
walk-forward consistency, max DD, hit rate, avg winner/loser. They
don't push any metric in either direction. A backtest report with a
high `open_trades / total_decisions` ratio is a warning that the
holding window is too long for the dataset, or the exit-quote stream
is missing data; the metrics shown are over the *closed* subset only.

### PnL boundary — three providers

The metric calculator does NOT compute realized R itself. It
receives a list of `RealizedTrade` from a `PnLProvider`
implementation. Phase 3.2.3 ships three:

| provider | what it does | when to use |
|---|---|---|
| `NoOpPnLProvider` | returns `realized_r=None`, `exit_reason="holding_window_open"` for everything | end-to-end wiring tests; `backtest report` default |
| `MockPnLProvider` | fixture-driven; returns whatever `RealizedTrade` was registered per `event_id` | unit tests of metric formulas with hand-built outcome streams |
| `SimplePnLProvider` | entry=ask, exit=bid via `ExitQuoteProvider`, slippage haircut, holding-strategy dispatch | the realistic-but-crude pricing for first real backtests |

`SimplePnLProvider`'s pricing model:

  * **Entry**: option `ask` at the decision's timestamp.
  * **Exit**: option `bid` at the holding-window close, looked up via
    an `ExitQuoteProvider`.
  * **Slippage**: `profile.backtest.slippage_pct` of entry premium,
    applied as a haircut to realized PnL. Default 2%.
  * **R units**: `1R ≡ entry premium`. Realized R = `(exit - entry -
    slippage) / entry × max_r`. With `max_r=0.5` (a smaller-bucket
    sized position) realized R is half magnitude.
  * **Holding strategies**:
      * `fixed_window` (default): close at `entry + holding_window_days`
        OR at `expiry - exit_on_dte_lte`, whichever first.
      * `dte_based`: close at `expiry - dte_based_close_threshold`,
        capped by the DTE floor.
      * `take_profit_or_stop`: raises `NotImplementedError` with a
        clear "Phase 3.4" message.
  * **No theta decay, no IV change, no underlying movement model.**
    Entry and exit prices are literal market quotes from the replay
    stream at those two timestamps.

This is intentionally crude. Real option pricing — Black-Scholes,
take-profit logic, volatility surface evolution — is Phase 3.4
territory. The point of `SimplePnLProvider` is consistency: if the
edge is real, it shows up here; if it doesn't show up here, a more
sophisticated pricer is unlikely to rescue it.

### MetricThresholds defaults — pinned

The default `MetricThresholds` (at module
`uoa_detector.backtest.metrics`) carries the Phase 3 prep step 3
numbers. Override-via-constructor is supported but the defaults
cannot drift without breaking
`test_track_b_thresholds_match_phase_3_prep`:

```
sharpe_pass = 1.5          sharpe_bonus = 2.5
expectancy_pass = 0.5      walk_forward_consistency_pass = 0.75
max_drawdown_ceiling = 0.20    min_trades = 60
```

The walk-forward threshold is N-independent by design: the same
0.75 means "6 of 8 at default N=8", "3 of 4 at N=4", "12 of 16 at
N=16". Profiles can change `walk_forward_windows` between backtests
without revising the threshold.

### CLI: `backtest report`

```sh
# Render a pass/fail table for a finished run.
python -m uoa_detector backtest report \
    --run-id <run_id> \
    --store sqlite:./run.db
```

The current default is `--pnl noop`: every trade reports as open,
every numerical metric is "insufficient_sample", `total_trades`
fails against `min_trades=60`. The point of running the report
today is to confirm wiring — that the run made it to the store and
the calculator + threshold + render path work end-to-end.

`--pnl simple` is reserved but raises a "Phase 3.3" deferral
message: it requires an exit-quote source wired from the replay
stream, which is the first real backtest's job to design. Until
then, real PnL math runs through the unit tests
(`tests/unit/test_simple_pnl.py`), not the CLI.

`--walk-forward-windows N` overrides the partition count for the
report only (the default 8 reads from the profile).

### Reproducibility

`compute_metrics` is deterministic — same input list, same output
`BacktestMetrics`, bit-identical. This is the precondition for
Phase 3.2.4's 4-cell runner, which compares metrics across cells
and would fail meaninglessly if the calculator drifted between
calls.

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

The 3.2.x series is the backtest framework's plumbing. After 3.2.3
the surface is: store + replay harness + PnL boundary + metric
calculator + a CLI report. Currently missing — by design — are:

  - **No latency measurement in the orchestrator yet.** The columns
    exist (`pipeline_latency_ms`, `data_source_latency_ms`), the
    round-trip is tested, but `Pipeline.process_one` does not
    currently populate them. Wiring lands in Phase 3.3 when the
    first real backtest report needs the value.
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
    source's incoming feed.
  - **`backtest report` is wired with `NoOpPnLProvider` only.** Real
    PnL math (`SimplePnLProvider`) needs an `ExitQuoteProvider`
    sourced from the replay stream — that surface is Phase 3.3's
    job. Today's report exercises the rendering + threshold compare
    paths; it always reports every trade as open.
  - **`take_profit_or_stop` holding strategy raises NotImplementedError.**
    The enum value is reserved in `BacktestConfig` so profiles can
    mention it; the implementation is Phase 3.4.
  - **No 4-cell combinatorial runner.** Phase 3.2.4 — single-source
    vs. multi-source × Tier-1 anchor universe vs. Tier-2.
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
