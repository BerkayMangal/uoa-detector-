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

The four key technical decisions in this document, captured here for
quick reference. Each is detailed in the relevant sub-commit section.

  1. **3.2.1 — Schema migration via Alembic.** Handcoded
     schema_version checks rejected. Adds `alembic` as a runtime
     dependency.

  2. **3.2.2 — Parquet compression: zstd level 3.** Sane default;
     revisit only if 3.3.x runs hit disk pressure.

  3. **3.2.3 — Real PnL deferred to 3.4. SimplePnLProvider shipped now**
     as the placeholder: entry = ask, exit = bid, slippage =
     `profile.backtest.slippage_pct` (default 2%), no theta/IV decay.
     Crude but consistent — if the edge is real, it shows up here.

  4. **3.2.4 — Tier-1 anchor universe: 20 tickers** (4 broad ETFs + 8
     mega-cap tech + 2 financials + 2 energy + 2 healthcare + 2 macro
     hedges). Approved list shipped in commit 3.2.0a alongside this
     acceptance doc, ready for use from 3.2.1 onward.

---

## Phase 3.2.1 — SQLite persistent BacktestStore

The current `BacktestStore` (Phase 1) holds events in a Python list. A
real backtest run can produce hundreds of thousands of decision records
across days of execution; in-memory-only is unviable. This commit adds a
SQLite-backed store as a drop-in replacement, behind the same Protocol,
so every existing test continues to pass with either backend selected.

**Schema.** Three tables: `backtest_runs` (run_id PK, started_at,
finished_at, profile_id, profile_content_hash, universe_filter,
fusion_filter, source_set, notes); `decision_records` (run_id FK,
event_id PK within run, ticker, ts, label, max_r, combined_score_pre,
combined_score_post, profile_id, profile_content_hash, full_record_json
TEXT); `schema_version` (single-row migration tracking, starts at 1).
Indexes on `decision_records(run_id, ts)` and `(run_id, ticker)` for
the metric calculator's expected access patterns. Migration uses
**Alembic** (approved decision; the alternative — handcoded
schema_version checks — accumulates technical debt fast). Even at v1
with no migrations to run, the framework is wired so v2/v3 migrations
have a clear path. New runtime dependency: `alembic` (added to
`pyproject.toml` in this commit).

**Drop-in compatibility.** The store exposes `start_run(profile,
universe_filter, fusion_filter, source_set, notes) -> run_id`,
`add(event, decision, size)`, `finish_run()`, `get_run(run_id)`,
`list_runs(...)`, and `iter_records(run_id, filters?)`. The orchestrator
takes a `BacktestStore` Protocol; the SQLite implementation satisfies it.
Every existing Phase 1-2 test that currently uses the in-memory store
must pass when the test fixture is parameterised over both backends —
this is the single most important acceptance check, written as a
parametrised test sweep at the end of the commit.

**Durability and concurrency.** `PRAGMA journal_mode=WAL` enabled at
open; verified by a unit test that queries `PRAGMA journal_mode` after
construction. `add()` uses batched writes (default flush threshold 100
records or explicit `finish_run()` flush, whichever first) so
high-throughput runs aren't I/O-bound. Append-only audit guarantee:
once a record is written it is never updated or deleted by the store
itself; tests verify this by attempting an `UPDATE` and expecting the
test-only path to fail (production code path has no update method).

**Done when:** all 12+ tests pass (including the parametrised
in-memory-vs-SQLite equivalence sweep — every Phase 1-2 test that
touches `BacktestStore` runs cleanly against both); WAL mode confirmed
via PRAGMA query in a test; mid-write process kill test recovers
cleanly on next open (WAL rollback works); CLI gains `--store
sqlite:path/to/db` flag with default `:memory:`; one new doc file
`docs/BACKTEST.md` introduces the storage layer.

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
implied_volatility, open_interest) plus `source_id`, `source_event_id`,
`arrival_ts` (the time the live system received the print, distinct
from the print's exchange timestamp). Parquet compression: `zstd` level
3 (approved default; revisitable in 3.3.x if disk pressure becomes a
problem — `zstd 9` for smaller files, `snappy` for faster reads).
Schema is pinned by a fixture file
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
timestamp, partitioned into 4 equal-trade-count quarters (not
equal-time, because Track B trade frequency varies); each quarter's E
computed independently; consistency = count of quarters where E > 0.
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
  3. `SimplePnLProvider` — the basic but consistent option-pricing
     model approved for Phase 3.2.3:
       - Entry price: option `ask` at the decision's timestamp
       - Exit price: option `bid` at the holding-window close
       - Slippage: `profile.backtest.slippage_pct` of entry premium,
         applied as a haircut to realized P&L. Default 2%; lives in
         a new `BacktestConfig` profile section.
       - No theta decay, no IV change, no underlying movement model —
         entry and exit prices are literal market quotes from the
         replay stream at those two timestamps.
     This is intentionally crude. Real option pricing — Black-Scholes or
     replay of full surface evolution — is Phase 3.4 territory. The
     point of `SimplePnLProvider` is consistency: if the edge is real,
     it shows up here; if it doesn't show up here, a more sophisticated
     pricer is unlikely to rescue it. The first-real-backtest in Phase
     3.3 uses `SimplePnLProvider`; downstream commits can swap in a
     better one without touching the metric calculator.

  The `BacktestConfig` profile section added in this commit:
  `slippage_pct: 0.02` (default), `holding_window_days: 7` (default —
  Track B horizon midpoint), with both fields tunable per profile.
  `v5_gamma_squeeze.yaml` is updated to inherit these defaults
  unchanged in this commit; explicit Track B tuning of slippage can
  happen in 3.3+ once we see real fills.

The PnL boundary is documented explicitly in `docs/BACKTEST.md` so
Phase 3.3's first backtest cannot silently use a misconfigured pricer.

**Pass/fail thresholds.** A `MetricThresholds` Pydantic model carries
the five Track B + Formülasyon A numbers approved in Phase 3 prep step
3: Sharpe pass `> 1.5` (`> 2.5` separate "bonus" flag), expectancy
pass `> 0.5R`, walk-forward consistency pass `>= 3`, max DD ceiling
`< 20%`, min trades `>= 60` over a 2-year backtest. These default
values are pinned by a test (`test_track_b_thresholds_match_phase_3_prep`);
override-via-constructor is supported but the defaults cannot drift
without breaking the test.

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
via `--walk-forward-windows N`). Each slice has an in-sample boundary
and an out-of-sample boundary; in Phase 3.2.4, in-sample is "data
seen, profile NOT tuned" — the same frozen profile runs everywhere.
(In-sample auto-tuning is Phase 3.4+; Phase 3.2.4's contribution is
the windowing infrastructure itself, with a dummy "no tuning" pass
function as the placeholder.) The walk-forward consistency metric in
Phase 3.2.3 reads slice-level Es from the SQLite store and rolls them
up.

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
