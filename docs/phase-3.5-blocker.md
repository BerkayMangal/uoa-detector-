# Phase 3.5 — Blocker discovery: the verdict engine was never wired

**Status: OPEN — needs Berkay's go/no-go before any code is written.**
**Date: 2026-08-11. Author: Claude Code (investigation, not implementation).**

This document is a finding, filed per discipline D1 ("if during
implementation you discover that a decision was wrong, you stop, you
flag it, you discuss with Berkay, and you write the discussion into a
new doc"). It changes no frozen contract and tunes no threshold. It
exists so the gap below is on the record before Phase 3.5 proceeds.

---

## One-line finding

The pipeline that turns downloaded historical data into trades and an
edge verdict — the thing every "Phase 3.3 will wire this" comment in
the code points at — **was never built.** M21–M28 scoring is real and
green; the download driver is real; the backtest *report* renderer is
real. The bridge between them that produces trades is a `noop` stub.

**Consequence:** running the Phase 3.5.5 command as written produces
**0 trades in all four cells**, before and after the historical
download. The download (8–48h of ThetaData bandwidth + Berkay's
laptop) would not yield a verdict, because no code path consumes the
downloaded data to produce trades.

---

## Evidence (verified at HEAD, main @ `04b9e2e`)

1. **`run-4cell` hard-rejects any real trade producer.**
   `src/uoa_detector/cli.py:751-757` — `if trades != "noop": raise
   BadParameter("--trades must be 'noop' in Phase 3.2.4 …")`.
   `cli.py:781` hardcodes `trade_producer=noop_trade_producer`.
   Its own docstring (`cli.py:746-749`) still reads *"Phase 3.2.4
   scope: orchestration + windowing + report rendering. Real trade
   producers … land in Phase 3.3."*

2. **The 4-cell runner has no channel to receive real trades.**
   `src/uoa_detector/backtest/cell_runner.py:279` — `trades =
   trade_producer(cell, windows, profile)`. The producer callable is
   handed only `(cell, windows, profile)` — no store, no data path,
   no replay stream. `noop_trade_producer` returns `[]`;
   `fixture_trade_producer` returns injected test trades. There is no
   third producer.

3. **The real PnL provider is never instantiated outside tests.**
   `grep 'SimplePnLProvider('` → only `tests/unit/test_simple_pnl.py`.
   `SimplePnLProvider` (`src/uoa_detector/backtest/simple_pnl.py:139`)
   works, but nothing in `src/` constructs it.

4. **No parquet-backed exit-quote source exists.**
   `ExitQuoteProvider` is a Protocol (`simple_pnl.py:59`). The only
   implementation is `DictExitQuoteProvider` (`simple_pnl.py:86`), a
   test mock. The docstring says the parquet/replay impl "Phase 3.3
   will wire one" — it was not wired. Without it, `SimplePnLProvider`
   has no exit prices, so every trade round-trips as `open`.

5. **`report --pnl simple` is explicitly disabled.**
   `cli.py:602-608` — `if pnl == "simple": raise BadParameter("…
   requires an exit-quote source wired from the replay stream. Not
   available in Phase 3.2.3 …")`. The only working PnL path is
   `NoOpPnLProvider` (`cli.py:623`), which reports every decision as
   open.

6. **The historical replay that DOES exist feeds the wrong half.**
   `ParquetReplaySource` is instantiated only at `cli.py:462`, inside
   the `run` (observe/detect) command's `--source=historical` path.
   That path replays parquet through the detection pipeline and can
   persist decision records to a store — i.e. the *signal-generation*
   half may work. The *PnL/exit* half (steps 3–5 above) is absent, and
   `run-4cell` does not consume replayed signals at all.

---

## Contract discrepancy (the D1 flag)

`docs/phase-3.5-acceptance.md` was written assuming an engine that
does not exist. Its commands reference CLI flags absent from the code:

- **Phase 3.5.4** (`acceptance:202-203`): `run-4cell --profile
  v5_gamma_squeeze --synthetic`. There is **no `--synthetic` flag**,
  and its "done-when" requires "each cell produced ≥ 1 trade" —
  impossible while the producer is `noop`.
- **Phase 3.5.5** (`acceptance:232-236`): `run-4cell … --replay-data
  data/historical/thetadata --start-date … --end-date … --output-dir
  …`. **None of `--replay-data`, `--start-date`, `--end-date`,
  `--output-dir` exist.** The real flags are `--store`,
  `--report-path`, `--from`, `--to`.

This is not a request to edit the frozen contract. It is a record that
the contract's Phase 3.5.4/3.5.5 steps cannot execute against the
current code, so the phase cannot proceed as written.

---

## What exists vs. what is missing

| Building block | State |
|---|---|
| Detection pipeline M1–M28 (+M35/M37) | ✅ built, green |
| `ParquetReplaySource` (historical replay) | ✅ built |
| `run --source=historical --store` (replay → signals → store) | ✅ appears wired (needs a live check) |
| `SimplePnLProvider` (premium-based R) | ✅ built, tested, **never used in prod** |
| Backtest store, metrics, 4-cell report renderer | ✅ built, green |
| **Parquet-backed `ExitQuoteProvider`** (ThetaData bid at exit_ts) | ❌ does not exist |
| **A trade producer** that reads stored signals → SimplePnL → RealizedTrades | ❌ does not exist |
| **`run-4cell` wiring** to a real producer / replay data | ❌ hardcoded `noop` |
| **`report --pnl simple`** wiring | ❌ disabled |

The parts are all there. The integration that makes them answer the
one binary question is what is missing.

---

## Proposed scope to unblock (needs approval — do NOT start without it)

A self-contained sub-phase (suggested name **Phase 3.5.0 — backtest
engine completion**), sized to be testable against the existing
synthetic parquet fixture before any real data or bandwidth is spent:

1. **`ParquetExitQuoteProvider`** — implements `ExitQuoteProvider`
   over the ThetaData v3 parquet schema (`backtest/parquet_schema.py`),
   returning the option bid at-or-before `exit_ts`. Mirrors
   `DictExitQuoteProvider` semantics.
2. **A real trade producer** — for each cell, replay the cell's
   universe through the pipeline (or read signals the replay wrote to
   the store), apply `SimplePnLProvider` with the parquet exit-quote
   source, return `RealizedTrade`s. This requires widening the
   producer's inputs (it currently gets only `cell, windows, profile`)
   — a `BacktestStoreProtocol`/API change, which per CLAUDE.md is a
   "must ask first" item.
3. **Wire `run-4cell`** to accept the real producer + a data path, and
   reconcile the CLI surface with what Phase 3.5.4/3.5.5 assume
   (`--synthetic`, `--replay-data`, date flags) — OR amend the phase
   steps to the real flags. Either way, a contract reconciliation.
4. **Wire `report --pnl simple`** to the same exit-quote source.
5. **Tests**: extend `test_cli_run_4cell.py` and add engine tests that
   assert ≥1 trade on the synthetic fixture (satisfies the real intent
   of Phase 3.5.4).

### Design decisions that are Berkay's call (money path)

These change whether a real or fake edge appears, so they are not
agent judgment calls:

- **Signal→trade mapping**: one trade per stored signal, or dedup per
  (ticker, contract, day)?
- **Holding strategy for the backtest**: `fixed_window` vs `dte_based`
  (profile already carries the knob; confirm which the verdict uses).
- **Exit-quote lookup tolerance**: how stale a bid is acceptable
  before a trade is marked `open` and excluded (illiquidity handling).
- **`max_r` / position sizing** semantics carried from the signal
  bucket into realized R.

---

## What was NOT done here (on purpose)

- No engine code written. Building an unreviewed backtest engine on
  the money path, untestable against real data from this environment,
  is exactly the failure mode CLAUDE.md gates behind Berkay's
  approval (phase progression + API change + strategy path).
- No thresholds touched. No frozen contract edited.
- The only change shipped alongside this finding is the independent
  runbook v2→v3 fix (merged in #2).

---

## Decision needed from Berkay

1. **Authorize Phase 3.5.0** (build the engine bridge, scope above),
   or reject/redirect. If authorized, next step is a frozen
   `docs/phase-3.5.0-acceptance.md` written before any code.
2. **Answer the four money-path design questions** above (can be
   deferred into the acceptance doc, but they must be pinned before
   implementation).

Until then, Phase 3.5.1's local validation and the historical
download remain premature: validating credentials and burning
bandwidth only pays off once the engine that consumes the data exists.
