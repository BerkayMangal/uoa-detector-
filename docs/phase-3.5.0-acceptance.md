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
