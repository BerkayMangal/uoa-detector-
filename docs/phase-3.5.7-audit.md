# Phase 3.5.7 — Sanity audit

Status: **PARTIAL — data-level checks complete; trade-level checks are
tooling-ready and run on the verdict run.**

The full audit is gated the same way the Phase 3.5.6 verdict is: the
trade-level checks (look-ahead leakage, trade frequency, PnL
distribution) need a run that actually produces closed trades. The
single-cell baseline (2026-06-09, v5_gamma_squeeze, no UW) produced
**zero closed trades** — nothing clears the 0.55 confluence threshold
without UW enrichment — so the trade-level checks have nothing to score
yet. They run on the eventual UW-enabled verdict run (B0).

The two checks that need only the downloaded data (slippage, survivorship)
are done now and recorded below. The tooling for all four computable
checks lives in `src/uoa_detector/backtest/sanity_audit.py` (pure
functions + 10 unit tests), so the moment the verdict run produces trades
the audit is mechanical.

---

## 1. Look-ahead leakage — TOOLING READY (runs on verdict run)

`sanity_audit.check_lookahead(trades)` asserts every **closed** trade
exits strictly after it enters (`exit_ts > entry_ts`, `exit_ts` present);
any closed trade failing this is a time-travel bug and is reported by
`event_id`.

Note on the contract: entry is at the signal timestamp, so
`event_ts == entry_ts` in this model — there is no entry delay to check.
The other half of the leakage contract ("no provider call uses data
later than `event_ts`") is enforced upstream **by construction**: the UW
providers were converted to as-of mode (Phase 3.5.5 B2 — `dealer_gamma`,
`iv_history`, `dark_pool`, `open_interest` select by event time, no
future rows) and fusion/decay operate on `event_ts` not wall-time (D9).
The spot-check of 10 random signals' provider timestamps is part of the
verdict-run audit.

**Finding:** pending the verdict run.

## 2. Survivorship / coverage — COMPLETE ✅

Checked the downloaded universe (24 tickers) against the 12-month window
2025-05 → 2026-04 for coverage gaps that would indicate a mid-period
delisting or halt.

- **24 / 24 tickers present, 12 / 12 months each (288 ticker-months).**
- **No zero-row months.** Total 290.9M prints.
- No coverage gap → no delisting/halt-driven survivorship bias **within
  the chosen universe** over the period.

Caveat: this confirms no survivorship bias *inside* the fixed universe.
It does not address selection of the universe itself (tickers chosen up
front in `data/universes/_bulk_all.csv`); that is a Phase 3.5.3 design
decision, not a backtest artefact.

## 3. Slippage sanity — COMPLETE ✅

The cost model (`simple_pnl.py`) enters at the **ask**, exits at the
**bid** (so it pays the full round-trip spread), and additionally applies
`backtest.slippage_pct = 0.02` (a 2% haircut on entry premium).

Measured `spread_pct = (ask - bid) / mid` on the 474,261 prints with
premium ≥ $100k (the candidate population the pre-filter would trade):

| stat | spread_pct |
|---|---|
| median | 1.24% |
| mean | 2.19% |
| p90 | 4.63% |
| p99 | 14.41% |
| > 15% (the profile's `wide_spread` penalty band) | 0.9% |

**Finding:** the execution-cost model is **not optimistic** — it pays the
full spread (median ~1.24% round-trip) **plus** the 2% haircut, so total
modelled cost (~3%+ for the median trade) sits above the realised spread.
The worst spreads (> 15%) are 0.9% of candidates and are already
penalised by the strategy's `wide_spread` rule (`spread_pct_threshold:
15.0`). The 0.02 assumption holds up; if anything it is slightly
conservative for the median trade.

Caveat: measured on the high-premium candidate population, not the
narrower set that would actually clear the 0.55 score (zero in the no-UW
baseline). Re-confirm on the verdict run's actual entry contracts.

## 4. Trade frequency — TOOLING READY (runs on verdict run)

`sanity_audit.assess_trade_frequency(n_closed)` classifies the best
cell's closed-trade count against the pinned band: `< 20` →
statistical-insignificance risk; `> 200` → backtest may be too easy;
target 60 (30/yr × 2yr). **Finding:** pending the verdict run (baseline
= 0 closed → INSUFFICIENT).

## 5. PnL distribution — TOOLING READY (runs on verdict run)

`sanity_audit.pnl_distribution(trades)` summarises realized R (n, mean,
median, stdev, min, max, win-rate) for an eyeball check against a
suspicious single-spike / bimodal shape. **Finding:** pending the verdict
run.

---

## Bottom line

No problem severe enough to invalidate a future verdict was found in the
data-level checks: coverage is complete and the slippage assumption is
sound. The trade-level checks are built and tested; they execute on the
UW-enabled verdict run (B0) and will be appended here. Until then Phase
3.5.7 stays PARTIAL.
