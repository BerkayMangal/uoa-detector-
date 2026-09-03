# Phase 3.5.0 — Backtest engine completion (acceptance contract)

**Status: FROZEN on Berkay's explicit approval (2026-08-11, "Onaylıyorum").**
**Prerequisite for: Phase 3.5.1 onward. Supersedes the implicit engine
assumption in `docs/phase-3.5-acceptance.md` §3.5.4/§3.5.5.**

This contract closes the gap documented in `docs/phase-3.5-blocker.md`:
the code that turns historical data into trades and a verdict was never
wired. Phase 3.5.0 builds that bridge and nothing else. Per D8/D4 it
tunes no thresholds; per D1 it does not edit the frozen Phase 3.5
contract — it reconciles it here, in a new doc.

---

## Objective

Make `run-4cell` produce **real trades** from historical parquet, so
the four-cell comparison report carries meaningful metrics. Success is
measured on the existing synthetic fixture (no real data, no bandwidth)
— once green there, the same engine reads real ThetaData parquet in
Phase 3.5.5.

---

## The trade this engine backtests (money mechanism, pinned)

This section makes the strategy's P&L loop explicit. It is a
restatement of what `profiles/v5_gamma_squeeze.yaml` and
`profiles/v5_default.yaml` already encode — not a new decision.

- **Instrument:** OTM short-dated calls. DTE 3–14, heart 0–7
  (`dte.bucket_0_7 = 1.30`); 15+ DTE heavily penalised, 60+ is a no-go.
- **Entry trigger:** a signal whose multi-source confluence score
  clears the Track B `label_thresholds` (CLUSTER/BURST, driven by
  dealer short-gamma + sweep + stealth OI accumulation).
- **Entry price:** option **ask** at decision time
  (`StoredSignal.option_price`).
- **Position size:** 1% risk per trade × the label bucket's `max_R`
  (`risk_buckets`): convexity_cluster / high_conviction_sequence =
  full 1%; standard_uoa = 0.25%; leap_positioning = 0.10%.
- **Exit rule:** `holding_strategy = fixed_window` — close at
  `entry + holding_window_days (5)` **or** when `DTE ≤ exit_on_dte_lte
  (2)`, whichever comes first. "Ignite in 3–5 days or get out before
  theta and expiry chaos."
- **Exit price:** option **bid** at exit, from the parquet stream.
- **Slippage:** 2% haircut on entry premium (`slippage_pct`).
- **Realized R:** `((exit_bid − entry_ask) − slippage) / entry_ask`,
  scaled by `max_R`. 1R = the premium paid.

**Why it is supposed to make money:** you buy cheap convexity right
before dealers who are net short gamma are forced into a reflexive
hedging loop. A small up-move makes them buy the underlying to stay
hedged, which pushes spot further, which forces more buying — a
squeeze. Cheap OTM calls then gain from delta and gamma at once. The
claimed edge is that multi-source confluence flags the setup early, in
Tier-2 (under-covered) names where fewer players compete.

**Whether it actually makes money is unproven.** That is the binary
question Phase 3.5.5 answers against the pinned targets: E > 0.5R/trade,
Sharpe > 1.5, ~30 trades/yr, 3-of-4 quarters positive, max DD < 20%.
This engine only makes the question *answerable*; it does not
presuppose the answer.

---

## Scope (bisectable sub-phases — each green on pytest+mypy+ruff)

### 3.5.0.1 — `ParquetExitQuoteProvider`
Implement `ExitQuoteProvider` (`backtest/simple_pnl.py`) over the
ThetaData v3 parquet schema (`backtest/parquet_schema.py`). `get_bid`
returns the option bid at-or-before `at`, or `None` when no quote is
within tolerance. Mirror `DictExitQuoteProvider` semantics
(walking-back lookup). Unit tests against the synthetic fixture parquet.

### 3.5.0.2 — Historical trade producer
A producer that, per cell: obtains the cell's stored signals (from the
replay the `run --source=historical --store` path already writes, or a
producer-internal replay), applies `SimplePnLProvider` with the
3.5.0.1 exit-quote source, and returns `list[RealizedTrade]`. This
requires widening the producer input beyond `(cell, windows, profile)`
to include the store/data handle — a deliberate, documented signature
change to `cell_runner.run_4cell_backtest` and its producer type.

### 3.5.0.3 — Wire `run-4cell` + reconcile CLI surface
Replace the hardcoded `noop` producer with a `--trades {noop,historical}`
selector (default stays `noop` for the plumbing tests). Add the data
inputs the historical producer needs. Reconcile flags with Phase 3.5
§3.5.5: accept the real names (`--from`/`--to`/`--store`/`--report-path`)
and add whatever `--replay-data`-equivalent the producer requires;
where §3.5.5 named a flag that does not fit, the real flag is
authoritative and the difference is recorded in the 3.5.0 closeout.

### 3.5.0.4 — Wire `report --pnl simple`
Enable the disabled branch (`cli.py:602`) to construct
`SimplePnLProvider` + the parquet exit-quote source over stored signals.

### 3.5.0.5 — Synthetic 4-cell proof + closeout
`run-4cell --trades historical` over the synthetic fixture produces
≥ 1 trade per cell, a byte-deterministic report, all falsification
sections numeric. This is the real Phase 3.5.4 intent. Paket-mode
closeout report.

---

## Design decisions pinned here (so implementation has no discretion)

1. **Signal → trade mapping:** one `RealizedTrade` per `StoredSignal`
   that took a position (`max_R > 0`). No cross-signal dedup in 3.5.0;
   if signal duplication proves to inflate trade counts, that is a
   Phase 3.5.7 audit finding, not an engine change.
2. **Exit-quote staleness tolerance:** if no bid exists at-or-before
   `exit_ts` within the same trading day, the trade is marked `open`
   (excluded from win/loss), never fabricated. Matches
   `DictExitQuoteProvider` returning `None`.
3. **Holding strategy / sizing / slippage:** taken from the profile as
   pinned above — not re-decided in code (D8).

---

## Done when

- 3.5.0.1–3.5.0.5 committed, each green.
- `run-4cell --trades historical` on the synthetic fixture yields ≥ 1
  trade per cell and a deterministic report.
- No profile threshold changed; no frozen contract edited.
- New tests cover the exit-quote provider, the historical producer,
  and the CLI wiring. Existing 1429 tests still pass.
- Paket-mode closeout appended here.

## Explicitly out of scope

- Real-data runs (that is 3.5.5, needs Berkay's download).
- Black-Scholes / take-profit-or-stop PnL (Phase 3.4+ pricer territory;
  `SimplePnLProvider` premium-based R is sufficient for the verdict).
- Any threshold tuning (D4).

---

# Phase 3.5.0 — CLOSEOUT (paket-mode)

**Status: KAPALI.** The backtest engine bridge is built. `run-4cell
--trades historical` and `report --pnl simple` now turn historical
parquet into real trades and metrics. No profile threshold changed; no
other frozen contract edited. The engine only makes the Phase 3.5.5
edge question *answerable* — it presupposes no answer.

## Commits shipped

1. `Phase 3.5.0.1: ParquetExitQuoteProvider over ThetaData v3 parquet`
2. `Phase 3.5.0.2: historical trade producer + 4-cell engine-proof fixture`
3. `Phase 3.5.0.3: wire run-4cell --trades {noop,historical}`
4. `Phase 3.5.0.4: wire report --pnl simple`
5. `Phase 3.5.0.5: synthetic 4-cell proof + closeout` (this commit)

## Verified-clean state (at the 3.5.0.5 commit)

- `uv run pytest -q` → **1461 passed, 20 skipped** (baseline was 1429;
  +32 new tests). Skips are the credential-gated integration smokes.
- `uv run mypy --strict src/` → **Success: no issues found in 112 source files**
- `uv run ruff check .` → **All checks passed!**

Every one of the five commits is independently green on all three.

## New tests (+32) and what they cover

- `tests/unit/test_parquet_exit_quote_provider.py` (14): walking-back +
  same-trading-day staleness, unknown contract/ticker, ticker filter,
  index caching, missing dir, and two `SimplePnLProvider` end-to-end
  cases (closes on a quoted day; opens off-data — never fabricated).
- `tests/unit/test_historical_trade_producer.py` (13): fixture-matches-
  generator pin; ≥1 closed trade per cell; the pinned winner/loser R
  values; Tier-1→SPY / Tier-2→PLTR universe routing; data-handle
  required; `run_4cell_backtest` integration + determinism; noop/fixture
  producers still accept the widened (optional) 4th arg.
- `tests/integration/test_cli_run_4cell.py` (+2 net): historical run
  produces ≥1 trade per cell with numeric E/max-DD; byte-deterministic
  report. Plus the reworked unknown-mode test and a
  historical-requires-`--replay-data` test.
- `tests/integration/test_cli_backtest_report.py` (+2 net): `--pnl
  simple` requires `--replay-data`; prices AAPL positioned signals as
  open (exits off-data); closes 4 real trades on the 4-cell fixture.

## Synthetic 4-cell proof (the real Phase 3.5.4 intent)

`run-4cell --trades historical --replay-data
tests/fixtures/historical/synthetic_4cell --from 2025-06-01 --to
2025-07-01 --walk-forward-windows 4`:

| cell | total trades | open | winner R | loser R | expectancy E | Sharpe | walk-forward |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| tier1_single | 2 | 2 | +0.408 | -0.442 | -0.017 | n/a | n/a |
| tier1_fusion | 2 | 2 | +0.408 | -0.442 | -0.017 | n/a | n/a |
| tier2_single | 2 | 2 | +0.408 | -0.442 | -0.017 | n/a | n/a |
| tier2_fusion | 2 | 2 | +0.408 | -0.442 | -0.017 | n/a | n/a |

Every cell produces ≥1 closed trade; E and max-DD are numeric (the
falsification-feeding metrics). Sharpe / walk-forward render `n/a`
because the sample (2 closed trades) is below their thresholds (30 for
Sharpe; N=4 windows need ≥4 closed) — that is the metric calculator's
existing, correct behavior, not a gap. The R numbers are a mechanical
consequence of the fixture (entry ask 2.00 → exit bid 3.00 / 1.00,
2% slippage, ×0.85 SWEEP_UOA `max_R`), **not evidence of edge** — the
fixture is hand-crafted plumbing, not market data.

## Judgment calls (numbered)

1. **[DEVIATION from the contract's fixture path — flagged per D1.]**
   The contract names the proof fixture
   `tests/fixtures/historical/synthetic/`. That directory is AAPL-only
   and **cannot** satisfy "≥1 (closed) trade per cell" for two
   independent reasons: (a) it holds no Tier-2 ticker, so Tier-2 cells
   get zero signals under the (correct) universe filter; (b) even for
   Tier-1, AAPL's 5-day `fixed_window` exits all land off-data (the
   fixture ends 2025-06-13; +5 days is a weekend/out-of-range), so
   every AAPL trade round-trips `open` and `total_trades` (closed only)
   is 0. It is also pinned to exactly 10 rows by
   `test_cli_historical.py`, so it cannot gain siblings. I therefore
   built a dedicated `tests/fixtures/historical/synthetic_4cell/`
   (SPY = Tier-1, PLTR = Tier-2; each with entry + exit-day quotes).
   This is a fixture-path deviation only — the engine, semantics, and
   thresholds are exactly as specified. Recommend Berkay ratify the
   path in a one-line amendment or accept it as recorded here.
2. **Staleness tolerance (pinned decision #2) made concrete:** "within
   the same trading day" = the resolved bid's UTC calendar date must
   equal `exit_ts`'s. Walking back across days is refused → `open`.
3. **Producer-internal replay** (over reading a store the `run
   --source=historical` path wrote): self-contained and testable
   without a pre-populated store. Signals are read back off
   `pipeline.store` via `list_runs()` + `iter_records()` because an
   empty in-memory `BacktestStore` is falsy (`__len__ == 0`) and the
   pipeline's `store or BacktestStore()` default silently swaps in its
   own — a pre-existing footgun documented at the call site.
4. **`cell.fusion` → `force_multi_source`:** `single` uses the fast
   path, `fusion` the windowed-fusion path. On the single synthetic
   source the two produce identical trades (there is only one source to
   fuse); the distinction is real for multi-source replay in 3.5.5. The
   deterministic, possibly-identical cells still satisfy the proof.
5. **Producer signature widening is backward compatible:** the 4th arg
   (`data: BacktestDataHandle | None = None`) defaults to `None`, so the
   `noop` / `fixture` producers and their existing call sites are
   untouched; only `run_4cell_backtest` always passes it.
6. **Pinned decision #1 applied on both surfaces:** the historical
   producer and `report --pnl simple` both price only positioned
   signals (`max_r > 0`); unpositioned decisions produce no trade.
7. **`--from`/`--to` drive walk-forward windowing only**, not replay
   month-clipping. `BacktestDataHandle.from_month`/`to_month` exist for
   that but the CLI leaves them unset (replays all months present for
   the universe). A dedicated replay-window flag can be added in 3.5.5
   if clipping is wanted; out of 3.5.0 scope.
8. **D10 test edits (behavior intentionally changed, not tests wrong):**
   `test_run_4cell_rejects_unknown_trades_mode` no longer asserts the
   old "Phase 3.3" deferral (historical is now valid) — it asserts an
   unknown mode is rejected and names the valid modes.
   `test_report_simple_pnl_deferred_to_phase_3_3` became
   `..._requires_replay_data` plus new happy-path coverage.

## CLI surface reconciliation (per 3.5.0.3)

Real flags are authoritative. `run-4cell` keeps
`--from`/`--to`/`--store`/`--report-path`/`--profile`/`--seed`/
`--walk-forward-windows`; adds `--trades {noop,historical}` (default
`noop`), `--replay-data <parquet root>` (required for historical), and
`--source-id`. `report` adds `--replay-data` and `--profile` for
`--pnl simple`. The Phase 3.5-acceptance names `--replay-data`,
`--start-date`, `--end-date`, `--output-dir`; the live names are
`--replay-data` (matches), `--from`, `--to`, `--report-path` — recorded
here rather than by editing the frozen Phase 3.5 contract.

## Sıradaki

Phase 3.5.1 (Berkay's local 7-step smoke validation) is now unblocked:
a real engine exists to consume the data. When the ThetaData historical
download (3.5.3) lands, `run-4cell --trades historical --replay-data
data/historical/thetadata ...` runs the same engine over real parquet
to produce the Phase 3.5.5 verdict. One open item for Berkay: ratify or
amend the fixture-path deviation in judgment call #1.
