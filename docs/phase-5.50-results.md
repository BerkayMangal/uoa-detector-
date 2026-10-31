# Phase 5.50 — actual data readiness result

DATA BLOCKED. No new best calibration, success probability or alpha was fitted.
The 438 candidates are a frozen plan, not 438 completed market backtests.
The user authorizes all proposed investigations; market-event inputs are the
remaining prerequisite, not another owner permission request.

## Actual observations

| Registered source | Actual local evidence | Intraday calibration limit |
|---|---|---|
| Study E | 133 rows, 10 tickers, 15 dates, 2026-06-16 through 2026-09-18 | Fields are ticker/day/print_count/mean_score/max_score; no dated side-premium events |
| Study F | 17,640 rows, 70 tickers, 252 dates, 2025-09-18 through 2026-09-18 | Daily OHLCV; no intraday signal/stop ordering or executable option marks |
| Existing trimmed option replay snapshots | 8 files across 2026-09-08 and 2026-09-22 | Not a continuous event-to-exit quote history |
| Existing local SQLite archive | 0 original live decisions, alerts, polls or sampled prices exported | Empty local development data is not production evidence |
| Current UW capability attempt | MISSING_CREDENTIAL, 0 HTTP requests | No present entitlement, historical depth or quota headroom is claimed |

The older vendor probe is dated 2026-09-23. It cannot prove current entitlement.
No reserved Study G price was opened. Sparse legacy data was not upgraded into
minute flow, no current classification was painted onto an old event, and no
synthetic test return is presented as a market result.

## Reproducible receipts

- artifacts/event-calibration-v1/readiness.json: real registered-source counts,
  checksums and limitations.
- artifacts/event-calibration-v1/capability-current.json: missing-credential
  attempt, zero requests; a saved secret requirement is not a working binding.
- artifacts/event-calibration-v1/observed-calibration.json: actual workbench run
  on the empty local export. DATA_BLOCKED, 438 planned candidates, 0 executed
  replays, winner null. It correctly exits 2 instead of reporting a fitted result.
- The original daily-source and earlier research reports remain unchanged.

## Prepared analysis

The new offline workbench implements first-bucket as-of sampling, conservative
poll coverage, same-ticker/time normalization, six explicit ablation arms,
chronological training/validation, an embargo, training-only shrinkage estimates,
selection before final replay, missing-label accounting, cost/delay diagnostics
and fixed-contract quote comparisons. Baseline is original recorded advice;
unknown source snapshots remain first and unusable rather than being replaced.

Stock outcomes are sampled reference observations, not fills. Expected returns
are reference percentages; risk-normalized diagnostics require the recorded
planned-risk scale. Stock stop risk is not a bounded loss. Auxiliary market
context may use the contemporaneously available five-minute SPY anchors;
sector/gamma require supplied dated input and are not invented by SQL export.
News checked-found does not assert positive sentiment. Quote packets are
receipt-dated quality B, with disclosed packet times, not exchange-timed fills.
Recorded scan completion is not post-commit publication proof; this is an
observational workbench, not a reconstruction of executed recommendations.

SQL export is SELECT-only and refuses a source over 50,000 rows rather than
silently truncating it. --since and --tickers bound a research extraction and
are recorded in provenance. Larger/full-market processing requires a separately
sized extraction; this bounded tool is not advertised as an unlimited harvester.
Bulk vendor harvesting remains unperformed pending verified access, completeness
and headroom. Deployment and binding changes remain independently verifiable.

## Additional actual quote-cost calculation

A separate descriptive calculation was frozen in phase-5.50-quote-cost-addendum.md
before inspecting its cost results. It uses the eight existing chain files;
4,442 quote structures were independently reconciled to source-leg cashflows.
There are still ZERO new event-return/option-return backtests or fitted winners.

| Saved-quote stratum, 14–45 DTE | Structures | Median round-trip cost / entry debit | One-unit debit <= USD100 | Both budget and <=25% cost |
|---|---:|---:|---:|---:|
| All long calls | 3,223 | 2.84% | 499 | 219 |
| Long calls, absolute delta .25–.75 | 598 | 1.75% | 0 | 0 |
| All adjacent call debit verticals | 1,219 | 63.49% | 1,022 | 99 |
| Verticals whose long leg has abs delta .25–.75 | 469 | 44.36% | 322 | 86 |

The budget is modeled entry cash, including opening commission, not a verified
broker risk requirement. Bid/ask crossing plus USD0.65 per contract/side is counted
once. The assumed standard multiplier is 100. Quote quality is B; exchange times,
leg concurrency and corporate adjustments are not verified. Negative leg-by-leg
closing credits are retained as costly reference arithmetic, not executable
negative-value verticals. Snapshot combinations overlap and are not independent.

This says why premium affordability and relative friction must be checked
separately. The delta stratum is not a profitability filter; it does not say any
of the 86 combinations should be bought, that 25% is optimal, or that a larger
risk budget is advisable. No forward returns were available for these quotes.
All source/profile/script hashes and the complete Decimal ledger are preserved
in artifacts/event-calibration-v1/quote-cost-audit.json.
