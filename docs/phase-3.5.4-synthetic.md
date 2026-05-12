# Phase 3.5.4 — Synthetic 4-cell pre-flight backtest

**Status: KAPALI.** Wiring validated end-to-end before the Phase
3.5.5 real-data run.

## Purpose

The Phase 3.5 acceptance contract pins:

> Before running the full backtest, confirm the 4-cell runner
> produces sensible output on the synthetic universe. This catches
> wiring bugs before they consume real-data wall-clock.

Specifically:

1. 4 `CellRunResult` outputs (one per universe × fusion combination)
2. Each cell produced ≥ 1 trade
3. Markdown comparison report has all 4 falsification sections
   populated
4. Walk-forward windows split correctly per cell

The synthetic numbers are NOT meaningful — only the plumbing.

---

## What changed

### Sub-commit 3.5.4.1 — `synthetic_trade_producer` + CLI `--trades synthetic`

`src/uoa_detector/backtest/cell_runner.py` gains
`synthetic_trade_producer`, a cell-agnostic producer that:

1. Builds a `SyntheticRawFlowSource` from
   `default_scenario_prints()` (8 prints across AAPL/MSFT/NVDA/
   TSLA/XYZ/SPY/GOOGL plus a XYZ-low-strike sample)
2. Wires it into a fresh `Pipeline` with `default_stage_pipeline()`
   (13 stages, M21-M28 + Phase 3.1 cluster decay)
3. Drains the pipeline run; every emitted `StoredSignal` is mapped
   to a `RealizedTrade` via `NoOpPnLProvider` (`realized_r=None`,
   `exit_reason="holding_window_open"`)
4. Returns the trade list to the 4-cell orchestrator

The CLI's `backtest run-4cell --trades` flag now accepts
`synthetic` in addition to the prior `noop`. The 4-cell smoke
loop dispatches on the flag.

### Sub-commit 3.5.4.2 — `Pipeline.__init__` bug fix

The synthetic producer surfaced a latent bug: `Pipeline.__init__`
had `self._store = store or BacktestStore()`. `BacktestStore` is
falsy when empty (`__len__ == 0`), so the
caller's freshly-constructed store was being discarded and replaced
by a private one. Any external code reading the post-run store
saw zero signals; only `pipeline.store.all()` would have worked.

Fix: explicit `None` check —
`self._store = store if store is not None else BacktestStore()`.

This change keeps every existing call site working (callers that
omitted `store=` still get an implicit store) and starts honouring
the explicit `store=` argument, which the 3.5.4 synthetic producer
relies on.

### Sub-commit 3.5.4.3 — tests + doc + commit

  - `tests/unit/test_cell_runner.py` gains two tests:
    `test_synthetic_trade_producer_emits_one_trade_per_signal`
    and `test_synthetic_trade_producer_is_cell_agnostic`.
  - This file (`docs/phase-3.5.4-synthetic.md`) committed.
  - The actual report at `reports/phase-3.5.4/comparison.md`
    will be regenerated on every smoke run; it is not committed
    (gitignored under `reports/`).

---

## Smoke result (v5_gamma_squeeze, 4 windows, 12-month period)

```bash
PYTHONPATH=src .venv/bin/python -m uoa_detector backtest run-4cell \
  --store ":memory:" \
  --report-path reports/phase-3.5.4/comparison.md \
  --from 2024-01-01 --to 2024-12-31 \
  --walk-forward-windows 4 \
  --profile profiles/v5_gamma_squeeze.yaml \
  --trades synthetic
```

```
=== 4-cell backtest complete — wrote reports/phase-3.5.4/comparison.md ===
  tier1_single   trades=   0 open=   8 E=+0.0000 FAIL
  tier1_fusion   trades=   0 open=   8 E=+0.0000 FAIL
  tier2_single   trades=   0 open=   8 E=+0.0000 FAIL
  tier2_fusion   trades=   0 open=   8 E=+0.0000 FAIL
```

**Each cell produces 8 open trades** (≥ 1 — Phase 3.5.4 invariant
satisfied). `total trades` reads 0 because the NoOp PnL provider
keeps every position open by design; the metric calculator's
"total" counts only closed trades. `open=8` confirms the
pipeline → store → producer → metrics chain is end-to-end live.

Every cell reports `FAIL` overall because metric thresholds
(Sharpe ≥ 1.5, walk-forward consistency ≥ 0.75, etc.) can't be
computed on 0 closed trades — the per-metric table shows
`⚠️ INSUFFICIENT` for sharpe / expectancy / walk-forward and
`✅ PASS` for max-DD (no losses to draw down from). This is the
expected Phase 3.5.4 outcome; real verdicts wait for Phase 3.5.5.

### Comparison-report contents

`reports/phase-3.5.4/comparison.md` renders the full
Phase 3.2.4.3 report format with:

  - 4-cell summary table (Sharpe, expectancy, walk-forward,
    max-DD, overall)
  - Per-metric pass/fail breakdown
  - Per-cell run metadata (run_id, open_trades, hit_rate)
  - Falsification section enumerating the four scenarios that
    reject the Formülasyon A combinatorial hypothesis

All four falsification scenarios are present in the report.

---

## What is NOT done by Phase 3.5.4

  - No real PnL math. `NoOpPnLProvider` returns open positions
    only; `SimplePnLProvider` (entry-at-ask, exit-at-bid,
    holding-window) wires up in Phase 3.5.5 when real ThetaData
    quotes are available.
  - No per-cell behavioural divergence. All four cells run the
    same producer; in 3.5.5, `cell.universe` (tier1_anchor vs
    tier2_starter) and `cell.fusion` (single vs unanimous) drive
    real differences.
  - No profile-tuning. The synthetic scenario is fixed; profile
    threshold sensitivity testing is out of Phase 3.5.4 scope
    (and would violate Phase 3.5's no-tuning discipline anyway).

---

## Calibration / falsification implications

None. The synthetic source produces fixture-grade events
unrelated to real flow distributions. Phase 3.5.4 is wiring
plumbing only; the falsification scenarios pinned in Phase 3.2.4
apply to Phase 3.5.5's real-data verdict, not this pre-flight
run.

---

## Sıradaki

Phase 3.5.4 KAPALI → Phase 3.5.3 (full Tier-2 historical
download) is the operator's responsibility — agent does not run
that command. Phase 3.5.5 (real-data 4-cell backtest) starts
once Phase 3.5.3's manifest is committed.

For Phase 3.5.5, `synthetic_trade_producer` becomes
`replay_trade_producer` (consumes parquet files from
`data/historical/`, builds a `ParquetReplaySource`, plugs into
the same pipeline + producer pattern, swaps NoOpPnLProvider for
`SimplePnLProvider` so trades actually close).
