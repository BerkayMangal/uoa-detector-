# Phase 3.2.x — Backtest framework acceptance criteria

This document defines, ahead of implementation, what "done" means for each
of the four sub-commits in the Phase 3.2.x series. Acceptance criteria are
written before code so we can disagree on them, refine them, and approve
them with a record — not retrofit them onto whatever happened to ship.

The four sub-commits land sequentially. Each is independently green
(`pytest -q`, `mypy --strict src/`, `ruff check .` all clean) and
bisectable. Together they form the backtest framework Phase 3.3 will use
to actually evaluate Track B + Formülasyon A on historical data.

The framework itself does not produce trading signals to act on. It
produces metrics that test the edge hypothesis. Phase 3.3 (first real
backtest) follows; Phase 3.4+ (modules) and Phase 3.5+ (live data
adapters) come after the edge has been validated or rejected.

## Approved decisions (review-cycle outcomes)

The key technical decisions in this document, captured here for quick
reference. Each is detailed in the relevant sub-commit section.

  1. **3.2.1 — Schema migration via Alembic.** Handcoded
     schema_version checks rejected. Adds `alembic` as a runtime
     dependency. Schema is **four tables**: `backtest_run` (run_id,
     started/finished_at, profile id+hash, universe_id,
     source_config_hash, total_signals_processed, total_errors,
     dataset_window_start/end, notes), `signal` (decision records
     plus `pipeline_latency_ms` and `data_source_latency_ms`),
     `backtest_run_error` (per-stage error log), and `schema_version`.
     The latency columns + error table back the Phase 3.3.x cross-cell
     comparison's "ran cleanly on the same window with the same
     profile hash" precondition.
     **Lifecycle:** `start_run` is permissive by default (implicit run
     on first `add`), strict opt-in via
     `BacktestStore(strict_run_lifecycle=True)` for 3.2.4's 4-cell
     runner. `schema_version` is SQLite-only; in-memory returns `None`.

  2. **3.2.2 — Parquet compression: zstd level 3.** Sane default;
     revisit only if 3.3.x runs hit disk pressure. Schema includes
     `replay_ts` alongside the existing `timestamp` and `arrival_ts`,
     so determinism diffs between runs can be debugged from the data
     alone.

  3. **3.2.3 — Real PnL deferred to 3.4. SimplePnLProvider shipped now**
     as the placeholder. Defaults: entry = ask, exit = bid, slippage
     = 2% (`profile.backtest.slippage_pct`), `holding_window_days = 5`
     (revised down from 7 — Track B trade dynamics are 3-5 days),
     `exit_on_dte_lte = 2` (forced exit before expiry-day chaos),
     `holding_strategy = fixed_window` (default; `dte_based` also
     implemented; `take_profit_or_stop` reserved in the enum but
     raises `NotImplementedError` with a "Phase 3.4" message).
     Crude but consistent — if the edge is real, it shows up here.

  4. **3.2.4 — Tier-1 anchor universe: 20 tickers** (4 broad ETFs + 8
     mega-cap tech + 2 financials + 2 energy + 2 healthcare + 2 macro
     hedges). Approved list shipped in commit 3.2.0a alongside this
     acceptance doc, ready for use from 3.2.1 onward.
     **Walk-forward windowing**: default N=8 (3-month slices over 2
     years). **Pass threshold expressed as fraction**, not count:
     `walk_forward_min_consistency_pct = 0.75` lives in the profile
     and survives N changing without re-tuning.

---

## Phase 3.2.1 — SQLite persistent BacktestStore

The current `BacktestStore` (Phase 1) holds events in a Python list. A
real backtest run can produce hundreds of thousands of decision records
across days of execution; in-memory-only is unviable. This commit adds a
SQLite-backed store as a drop-in replacement, behind the same Protocol,
so every existing test continues to pass with either backend selected.

**Schema.** Four tables.

  * **`backtest_run`** — one row per backtest invocation. Columns:
    `run_id` PK, `started_at`, `finished_at`, `profile_id`,
    `profile_content_hash`, `universe_id` (e.g., `tier1_anchor` or
    `tier2_starter`), `source_config_hash` (hash of which sources
    were active and their config — Polygon/UW/IBKR enable flags,
    fusion threshold, etc.), `total_signals_processed`,
    `total_errors`, `dataset_window_start`, `dataset_window_end`,
    `notes`. Phase 3.3.x cross-cell comparison ("cell 4 beat cell 1")
    requires proving both cells ran on the same dataset window with
    the same profile content_hash and zero errors — these columns
    are the audit trail that backs that proof.

  * **`signal`** — one row per processed event (renamed from earlier
    draft's `decision_records` for brevity). Columns: `run_id` FK,
    `event_id` PK within run, `ticker`, `ts`, `label`, `max_r`,
    `combined_score_pre`, `combined_score_post`, `profile_id`,
    `profile_content_hash`, `pipeline_latency_ms` (end-to-end stage
    chain wall-clock), `data_source_latency_ms` (time from upstream
    source emission to orchestrator entry — how stale was the data
    when we acted on it), `full_record_json` TEXT. The two latency
    columns matter for backtest realism: a pipeline that takes 800ms
    end-to-end produces signals you'd never act on in live trading,
    and the metric calculator should be able to flag that.

  * **`backtest_run_error`** — one row per stage error encountered
    during a run. Columns: `run_id` FK, `event_id` (nullable — some
    errors happen before an event_id is assigned), `stage_name`,
    `error_type`, `error_message`, `occurred_at`. Phase 1-2 errors
    were logged but not persisted; backtests that ran "successfully
    with 200 silently-skipped events" need to be visible. The metric
    calculator reads `total_errors` from `backtest_run` for the
    cross-cell comparison's "ran cleanly" precondition; the detail
    rows in this table back that count.

  * **`schema_version`** — single-row migration tracking, starts at
    1. Alembic-managed.

Indexes: `signal(run_id, ts)`, `signal(run_id, ticker)`,
`backtest_run_error(run_id, occurred_at)`. Migration uses
**Alembic** (approved decision; the alternative — handcoded
schema_version checks — accumulates technical debt fast). Even at v1
with no migrations to run, the framework is wired so v2/v3 migrations
have a clear path. New runtime dependency: `alembic` (added to
`pyproject.toml` in this commit).

**Drop-in compatibility.** The store exposes:

  * `start_run(profile, universe_id, source_config_hash,
    dataset_window_start, dataset_window_end, notes) -> run_id`
  * `add(event, decision, size, pipeline_latency_ms,
    data_source_latency_ms)` — the two latency args default to None
    so existing call sites that don't measure latency keep working
  * `record_error(event_id, stage_name, error_type, error_message)`
    — increments `backtest_run.total_errors` and inserts a row in
    `backtest_run_error`
  * `finish_run()` — sets `finished_at`, flushes batched writes
  * `get_run(run_id) -> RunMetadata | None`
  * `list_runs(...)` — filterable
  * `iter_records(run_id, filters?)` — streaming
  * `iter_errors(run_id) -> Iterator[ErrorRecord]`

The orchestrator takes a `BacktestStore` Protocol; the SQLite
implementation satisfies it. Every existing Phase 1-2 test that
currently uses the in-memory store must pass when the test fixture
is parameterised over both backends — this is the single most
important acceptance check, written as a parametrised test sweep at
the end of the commit. The in-memory store gets the new latency and
error fields too (kept in lists alongside the existing decision list)
so the Protocol stays unified.

**Durability and concurrency.** `PRAGMA journal_mode=WAL` enabled at
open; verified by a unit test that queries `PRAGMA journal_mode` after
construction. `add()` uses batched writes (default flush threshold 100
records or explicit `finish_run()` flush, whichever first) so
high-throughput runs aren't I/O-bound. Append-only audit guarantee:
once a record is written it is never updated or deleted by the store
itself; tests verify this by attempting an `UPDATE` and expecting the
test-only path to fail (production code path has no update method).

**Done when:** all 16+ tests pass (including the parametrised
in-memory-vs-SQLite equivalence sweep — every Phase 1-2 test that
touches `BacktestStore` runs cleanly against both); WAL mode confirmed
via PRAGMA query in a test; mid-write process kill test recovers
cleanly on next open (WAL rollback works); latency-column round-trip
test (write known `pipeline_latency_ms` + `data_source_latency_ms`,
read back, assert preserved); error-table round-trip test (record
3 stage errors, `iter_errors` yields all 3, `total_errors=3` on the
run row); CLI gains `--store sqlite:path/to/db` flag with default
`:memory:`; one new doc file `docs/BACKTEST.md` introduces the
storage layer.

**Post-implementation clarifications (added during 3.2.1 design,
before code):**

  * **`start_run` lifecycle defaults to implicit; strict mode is
    opt-in.** Permissive default: the first `add()` call without a
    prior `start_run()` auto-creates an "implicit" run and subsequent
    `add()` calls write to it. Phase 1-2 tests keep working without
    modification. Strict mode is enabled per-store via
    `BacktestStore(strict_run_lifecycle=True)`; in strict mode an
    `add()` without a preceding `start_run()` raises
    `RunLifecycleError`. Phase 3.2.4's 4-cell runner uses strict
    mode so each cell gets a separate `run_id` with no
    cross-contamination from implicit-run leakage. The pattern
    follows Phase 2's `sub_score_missing_behavior` precedent:
    permissive default for the common case, strict opt-in for the
    machinery that needs the guarantees. The parametrised test sweep
    covers both modes: `test_implicit_run_default_behavior`,
    `test_strict_lifecycle_raises_without_start_run`,
    `test_strict_lifecycle_accepts_explicit_start_run`.

  * **`schema_version` is SQLite-only; in-memory returns `None`.**
    The Protocol exposes `schema_version: int | None`. The SQLite
    implementation returns the current Alembic-tracked version
    (starts at 1, climbs as migrations land). The in-memory
    implementation returns `None` — there is no persistent state to
    migrate, the value would be meaningless. Tests assert both
    branches: `in_memory.schema_version is None` and
    `sqlite.schema_version >= 1`.

  * **`finish_run()` and `close()` are distinct lifecycle methods.**
    `finish_run()` ends the active run (sets `finished_at`, flushes
    batched buffers) but the store stays usable for the next
    `start_run()`. `close()` flushes, finalises any active run, AND
    disposes the engine; subsequent calls raise `RuntimeError`. The
    separation exists so Phase 3.2.4's 4-cell runner can reuse one
    store across four cells (one trailing `close()` after the loop)
    without being forced into "one SQLite file per cell". The
    orchestrator's `Pipeline.run()` calls `finish_run()` on its
    `finally` block but never `close()` — the store's lifecycle
    belongs to whoever constructed it (CLI, 4-cell runner, test
    fixture), not the orchestrator. Tests pin this contract:
    `test_finish_run_does_not_close_store`,
    `test_pipeline_run_does_not_close_store`,
    `test_close_makes_store_unusable`,
    `test_close_is_idempotent`,
    `test_pipeline_run_with_strict_store_explicit_start_run`.

---

## Phase 3.2.2 — Replay harness

The replay harness is the historical-data analogue of
`SyntheticRawFlowSource`. It reads `RawPrint` records from disk and
emits them as a stream the existing `SourceFusion` and orchestrator
consume unchanged. Zero changes to upstream code: the harness satisfies
the `RawFlowSource` Protocol exactly, so the orchestrator cannot tell
synthetic data from historical replay apart at the boundary.

**Format pin.** Parquet, one file per `data/historical/{source}/{ticker}/{YYYY-MM}.parquet`.
Schema: every field of the canonical `RawPrint` Pydantic model
(timestamp, ticker, option_type, strike, expiry, dte, spot_price,
premium_paid, option_price, bid, ask, fill_side, exchange, is_iso,
implied_volatility, open_interest) plus three time-tracking fields:
`source_id`, `source_event_id`, `arrival_ts` (the time the live system
received the print, distinct from the print's exchange `timestamp`),
and `replay_ts` (the walltime the replay harness emitted this row in
the current run). The triple `timestamp + arrival_ts + replay_ts` is
the audit trail for replay determinism debugging: when two backtest
runs over the same dataset window produce different signal counts, the
diff between their `replay_ts` distributions points at where the
non-determinism leaked in (file order, k-way merge, async scheduling).
`replay_ts` is written by the harness at emission time, NOT by the
upstream data source; the source files on disk leave this column NULL
and the harness fills it during read. Parquet compression: `zstd`
level 3 (approved default; revisitable in 3.3.x if disk pressure
becomes a problem — `zstd 9` for smaller files, `snappy` for faster
reads). Schema is pinned by a fixture file
`tests/fixtures/historical/synthetic/AAPL/2025-06.parquet` containing
10 hand-crafted records that the harness loads at test time; if anyone
adds a new `RawPrint` field, that test fails until the fixture is
regenerated, which is the right kind of friction.

**Ordering guarantees.** Each per-(source, ticker, month) file is
**event-time-monotonic** by `timestamp` ascending — the harness assumes
this and the schema validator enforces it on load (out-of-order rows
raise `DataIntegrityError` at file open, not at iteration). Multi-file
playback uses k-way merge across all `(ticker, month)` files for a
given source, so the consumer sees a single timestamp-ordered stream.
Multi-source replay (the Formülasyon A scenario where Polygon + UW +
IBKR are all replaying simultaneously) wires three harnesses into
`SourceFusion` and lets the existing watermark logic do its job — no
new fusion code; the replay harness's contract is "behave like a live
source, just faster".

**Speed control and filtering.** `replay_speed` parameter: `inf`
(default, batch processing) or a positive float for real-time pacing
(`1.0` real-time, `10.0` 10× faster than walltime). Universe filter:
optional `Iterable[str]` argument; when set, only files for those
tickers are opened. Date range filter: `from_date` and `to_date`
arguments restrict the file glob and trim the within-file rows.

**Done when:** all 10+ tests pass including a small-sample integration
(5 tickers × 1 month: harness reads 50 files worth of synthetic data,
SourceFusion produces a single ordered stream, orchestrator processes
each event and writes to SQLite store, total record count matches);
fusion replay test (3 sources × 2 tickers × 1 month: tier classifier
sees `unanimous`/`majority`/`single`/`conflicted` mix as expected from
multi-source data); CLI gains `--source historical --data-dir
data/historical --tickers <list> --from <YYYY-MM> --to <YYYY-MM>`;
`docs/BACKTEST.md` documents the file format and directory convention
with a worked "how to provide your own data" example.

---

## Phase 3.2.3 — Metric calculator

This commit converts a stream of `SignalDecisionRecord` into the five
success metrics the Phase 3 prep approved as Track B + Formülasyon A's
pass/fail criteria. The math is fixed here so subsequent commits and
Phase 3.3's first real backtest cannot quietly redefine "did the edge
work" — the formulas are pinned by tests.

**Formulas.** *Sharpe (annualized):* daily realized-R series; mean
divided by stdev, multiplied by √252. Returns `None` if total trades <
30 (insufficient sample). *Expectancy E:* simple arithmetic mean of
per-trade R. *Walk-forward consistency:* trades sorted by exit
timestamp, partitioned into N equal-trade-count windows (N from
`profile.backtest.walk_forward_windows`, default 8 — see 3.2.4); each
window's E computed independently; consistency = **fraction of windows
where E > 0** (a real number in [0, 1], not an integer count). The
pass threshold is then expressed as a fraction
(`walk_forward_min_consistency_pct`, default 0.75) so the same
threshold survives if N is later changed to 4 or 16. With N=4, 0.75
means 3-of-4; with N=8, it means 6-of-8 or better.
*Max drawdown (%):* peak-to-trough on the cumulative-R curve, expressed
as percentage of the running peak. *Total trades:* count of decision
records whose label maps to a non-zero `max_r` bucket and whose realized
R is non-null. *Hit rate:* fraction of total trades where realized R >
0. *Avg winner R:* mean realized R over winning trades only (None if
zero winners). *Avg loser R:* mean realized R over non-winning trades
(zero winners → uses all trades).

**PnL boundary.** The metric calculator does NOT compute realized R
itself. It receives a `PnLProvider` Protocol implementation that maps
each decision record to a realized R (or None for "not yet exited /
out of holding window"). Phase 3.2.3 ships THREE implementations:

  1. `MockPnLProvider` — returns fixture R values per event_id, used by
     the metric-calculator tests to exercise every formula edge case.
  2. `NoOpPnLProvider` — returns None for every record, used by
     end-to-end tests that exercise the SQLite store + metric calculator
     wiring without taking a position on pricing.
  3. `SimplePnLProvider` — the basic-but-consistent option-pricing
     model approved for Phase 3.2.3:
       - Entry price: option `ask` at the decision's timestamp
       - Exit price: option `bid` at the holding-window close
       - Slippage: `profile.backtest.slippage_pct` of entry premium,
         applied as a haircut to realized P&L. Default 2%; lives in
         a new `BacktestConfig` profile section.
       - Forced exit at expiry: `profile.backtest.exit_on_dte_lte`
         (default 2) — when the option's DTE drops to this threshold
         or below, position closes immediately at that day's bid
         regardless of the holding strategy. This avoids modeling
         expiry-day gamma chaos / pin risk / assignment scenarios
         that the simple pricer cannot represent fairly.
       - No theta decay, no IV change, no underlying movement model —
         entry and exit prices are literal market quotes from the
         replay stream at those two timestamps.
     This is intentionally crude. Real option pricing — Black-Scholes,
     stop/take-profit logic, or replay of full surface evolution — is
     Phase 3.4 territory. The point of `SimplePnLProvider` is
     consistency: if the edge is real, it shows up here; if it doesn't
     show up here, a more sophisticated pricer is unlikely to rescue
     it. The first-real-backtest in Phase 3.3 uses `SimplePnLProvider`;
     downstream commits can swap in a better one without touching the
     metric calculator.

  **Holding strategy enum.** The `BacktestConfig` profile section
  introduces a `holding_strategy` field with three values:

    * `fixed_window` (default) — close after `holding_window_days`
      walltime, OR at `exit_on_dte_lte` boundary, whichever first.
      Track B's 3-5 day "ignite or die" dynamic: 7 days of theta
      bleed for a non-igniting position is unnecessary loss.
    * `dte_based` — close as soon as DTE drops to a configured
      threshold (separate from `exit_on_dte_lte`, which is a hard
      safety floor). Useful for "ride the squeeze until a few days
      to expiry, then bail" patterns. 3.2.3 implements this branch.
    * `take_profit_or_stop` — implementer raises `NotImplementedError`
      in 3.2.3. The enum value is reserved in the schema so
      profiles can mention it ahead of Phase 3.4 arriving with the
      real implementation. Tests verify the NotImplementedError
      surfaces with a clear "Phase 3.4" message.

  **`BacktestConfig` defaults pinned in this commit:**
    - `slippage_pct: 0.02`
    - `holding_strategy: fixed_window`
    - `holding_window_days: 5`  (revised from earlier draft's 7;
      Track B trade dynamics are 3-5 days "explode or die", and
      7 days yields unnecessary theta to non-igniting positions)
    - `exit_on_dte_lte: 2`
    - `walk_forward_min_consistency_pct: 0.75`  (pinned here for
      cross-reference; see 3.2.4 walk-forward section)

  `v5_gamma_squeeze.yaml` inherits these defaults unchanged in 3.2.3;
  explicit Track B tuning can happen in 3.3+ once we see real fills.

The PnL boundary is documented explicitly in `docs/BACKTEST.md` so
Phase 3.3's first backtest cannot silently use a misconfigured pricer.

**Pass/fail thresholds.** A `MetricThresholds` Pydantic model carries
the five Track B + Formülasyon A numbers approved in Phase 3 prep step
3: Sharpe pass `> 1.5` (`> 2.5` separate "bonus" flag), expectancy
pass `> 0.5R`, walk-forward consistency pass `>= 0.75`
(read from `profile.backtest.walk_forward_min_consistency_pct`,
N-independent — survives windows-count changes between backtests),
max DD ceiling `< 20%`, min trades `>= 60` over a 2-year backtest.
These default values are pinned by a test
(`test_track_b_thresholds_match_phase_3_prep`); override-via-constructor
is supported but the defaults cannot drift without breaking the test.

**Reproducibility.** Same input record stream → bit-identical metric
output, modulo float precision (asserted to 1e-9 tolerance). Test
sweeps a synthetic 200-trade dataset with known hand-computed metrics
and asserts every output value matches the expected.

**Done when:** all 12+ tests pass; threshold defaults pinned;
reproducibility test passes; CLI gains `python -m uoa_detector backtest
report --run-id <X>` that reads from SQLite and prints a pass/fail
table; `docs/BACKTEST.md` includes the formula definitions and the
PnL boundary explanation.

---

## Phase 3.2.4 — Walk-forward orchestrator + 4-cell combinatorial runner

The capstone of the 3.2.x series. Combines the previous three pieces
into a single CLI command that runs the Formülasyon A 4-cell
combinatorial backtest end-to-end: replay × universe filter × fusion
filter × walk-forward windowing → SQLite stores → metric calculator →
comparison report.

**Walk-forward windowing.** The full backtest period is split into N
equal-time slices (default N=8 over 2 years = 3-month slices, tunable
via `--walk-forward-windows N` and `profile.backtest.walk_forward_windows`).
Each slice has an in-sample boundary and an out-of-sample boundary; in
Phase 3.2.4, in-sample is "data seen, profile NOT tuned" — the same
frozen profile runs everywhere. (In-sample auto-tuning is Phase 3.4+;
Phase 3.2.4's contribution is the windowing infrastructure itself,
with a dummy "no tuning" pass function as the placeholder.) The
walk-forward consistency metric in Phase 3.2.3 reads slice-level Es
from the SQLite store and rolls them up as a **fraction of windows
positive** (not an integer count); the pass threshold is
`profile.backtest.walk_forward_min_consistency_pct = 0.75`, which
maps to "6 of 8" at the default N=8 and stays meaningful if a future
backtest uses N=4 (3 of 4) or N=16 (12 of 16). The fraction-not-count
representation is the explicit reason the threshold survives changes
in window count without re-tuning.

**4-cell matrix.** Four runs back to back: `(Tier-1, single)`,
`(Tier-1, fusion=unanimous)`, `(Tier-2, single)`, `(Tier-2,
fusion=unanimous)`. Each run produces a separate `run_id` in SQLite
with metadata flagging its cell. The Tier-2 universe is the
already-committed `data/universes/tier2_starter.csv`. The Tier-1
universe is `data/universes/tier1_anchor.csv`, committed earlier in
the 3.2.0 batch (alongside the acceptance doc) so it's available from
3.2.1 onward for tests and CLI smoke runs. The 20-ticker list is the
approved Phase 3 prep choice:

  Broad ETFs: SPY, QQQ, IWM, DIA
  Mega-cap tech: NVDA, AMD, AAPL, MSFT, GOOGL, TSLA, META, AMZN
  Financials: JPM, BAC
  Energy: XOM, CVX
  Healthcare: JNJ, UNH
  Macro hedges: GLD, TLT

Sector-diverse, all with options volume > 100k contracts/day, all
broadly watched. The list itself is a calibration artifact; the pin
test in `tests/unit/test_tier1_anchor_universe.py` (also shipped in
the 3.2.0 batch) asserts ticker count, schema match, and absence of
"flag for review" notes (Tier-1 is curated, no edge cases). When 3.2.4
lands, it adds an integration test that asserts both Tier-1 and
Tier-2 universes load cleanly through the same loader code path.

**Comparison report.** Markdown output at the path passed to
`--report-path`, with: a 4×5 metrics table (cell × metric); a
pass/fail flag per cell against the Track B thresholds; a "delta vs
Tier-1+single baseline" column for the other three cells (raw
difference, no significance test yet — significance testing is Phase
3.3 territory because it requires more thought about multiple-comparison
correction). The report includes a "what would falsify Formülasyon A"
section that restates the hypothesis: edge requires `(Tier-2, fusion)`
to materially beat the other three cells; partial wins (only `Tier-2,
single` beats baseline, or only `fusion` beats baseline) reject the
combinatorial hypothesis even if individual signals look interesting.

**Reproducibility.** A single `--seed` flag governs every random
choice (currently none — the 3.2.x pipeline is fully deterministic —
but the flag is wired so 3.4+ tuning can use it). Determinism test:
run the 4-cell suite twice with the same inputs, assert the SQLite
content_hash of decision records is identical across runs.

**Done when:** all 10+ tests pass; one CLI command produces the full
4-cell × 5 metric matrix; comparison report emitted to disk in
markdown; Tier-1 anchor universe loaded cleanly (file already
committed in the 3.2.0a batch — see acceptance preamble — integration
test verifies Tier-1 and Tier-2 share the loader code path);
determinism test passes; `docs/BACKTEST.md` documents the
walk-forward methodology and 4-cell matrix interpretation in enough
detail that a reader can answer "what would make Phase 3 conclude the
edge is real" without consulting other files.

---

## Cross-cutting acceptance — applies to every commit in 3.2.x

Every commit:
- `pytest -q`, `mypy --strict src/`, `ruff check .` all green before commit
- Bisectable: each commit is independently functional, reverting any
  single commit produces a working tree (we don't accept "commit C
  fixes commit B's bug" patterns)
- New tests pin behaviour, not implementation: tests assert "given
  input X, output is Y", not "function calls helper Z three times"
- Public API surface declared in `__all__`; new types exported from
  `__init__.py` so import paths are stable
- No new dependencies without explicit approval — current expected
  additions: `alembic` (SQLite migrations) in 3.2.1, nothing else
- Commit messages follow the established Phase 3 short-form pattern:
  one-line subject + bullet body + closing line confirming test count
  and cleanliness

When all four sub-commits land, Phase 3.2 is complete and Phase 3.3
(first real backtest) can begin.
