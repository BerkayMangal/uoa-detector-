# Phase 5.51 — actual historical diagnostics, 2026-11-01

**No validated alpha. No selected winner or live rule change.** These are
exploratory historical underlying-reference controls with assumed event-time
availability, not original LIVE/READY/PAPER recommendations or option returns.

## What actually ran

The newly available UW key was used on verified native endpoints. The erroneous
stock `/historic` capability probe was corrected to `/option-chains` without
rewriting the old receipts. Historical flow entitlement starts **2026-06-24**;
the registered end remains **2026-09-18**, before Study G's forward window. No
Study G price file was opened. The current access floor supersedes the old
95-session entitlement observation; it does not change past verdicts.

- NFLX, AAPL, NVDA; SPY only as matched reference.
- 61 source sessions; 20 warmup; 41 evaluated sessions.
- 641 bounded native HTTP requests; 614 sealed, checksummed packets. Remaining
  daily native quota at completion: 28,947. No paid plan or limit increase.
- 17,356 alert rows, 194,845 minute rows (including extended hours), 91,500 dated
  chain rows, 2,664 daily context rows. Chain rows are daily snapshots, not a
  complete executable quote surface; they were not used to invent option P&L.
- Resume checksum-verified all 614 packets with **zero additional HTTP requests**;
  original run counters are retained separately rather than reset or lost.
- Twelve fixed replays: three arms times four cost assumptions. Three thousand
  shared within-ticker/day timing resamples are controls, not independent
  strategies, fresh holdouts or formal p-values.
- Full 438-candidate walk-forward calibration **did not run**. The original
  80-session support gate was retained, not relaxed to manufacture a winner.

## Fixed base-cost results

All entries use independent $2,500 maximum whole-share reference units, first
eligible ticker/day, at most four units/day, one-hour holding, strictly later
native minute opens after a one-minute delay, exact same-timestamp SPY reference,
and 5 bp modeled slippage per side. Totals below are sums of hypothetical units,
**not cash-account P&L or fills**. Benchmark return is cost-adjusted and matched
to each unit's debit; it is not whole-account SPY buy-and-hold.

| Fixed arm | Resolved units | Gross move, same units $ | Modeled price cost $ | Net $ | Matched SPY excess $ | Net after best unit removed $ |
|---|---:|---:|---:|---:|---:|---:|
| Baseline flow/price proxy | 85 | -204.27 | 202.30 | -406.57 | -141.73 | -436.93 |
| Same-time normalized flow | 94 | -174.42 | 224.28 | -398.70 | -114.41 | -433.07 |
| Normalized flow plus price | 77 | -149.93 | 183.09 | -333.02 | -101.60 | -355.26 |

All twelve cost rows are retained in `artifacts/vendor-history-v1/report.json`;
all are negative. The gross column is algebraic attribution of these exact
fixed units, not a new zero-cost candidate selection. These controls lose even
before the modeled price cost. No direction was flipped after seeing returns.

The base-cost descriptive five-day block-resample intervals for mean daily
matched-SPY excess are [-10.510, +2.463], [-8.836, +2.360] and [-8.030, +2.051]
dollars respectively. They are not fresh statistical evidence or win odds.

## Timing and concentration controls

Each fixed ticker/day selection was compared with 1,000 seeded random intraday
entry schedules using the same costs, unit budget, delay and holding period.
One draw map is shared across all arms. All 1,000 draws per arm had exact marks;
no redraw-until-favorable or missing-data substitution occurred. The fraction
of valid null scores below actual was **2.8%, 8.4%, 7.4%**, respectively: actual
timing was poor under this descriptive control. These fractions are not p-values
or success probabilities. Random times do not preserve every volatility and
liquidity condition. Removing the best one/three units does not rescue any arm.

## Independent reconciliation and limitations

A separate auditor, without calling the diagnostic's pricing function, verified
all 614 sealed packet checksums and all **1,024 reference units across costs**
against native minute opens, SPY timestamps, sizing, costs, one-ticker/day limits,
delay/horizon bounds, totals and best-one/three removals. The original report was
not overwritten. Source state/capture provenance and both calculation scripts'
hashes are retained. Tampered or internally rehashed false ledgers are tested.

Native alert `created_at` and final aggregation `end_time` plus a declared
one-minute lag are **assumed** availability. Actual packet receipt times are in
November and remain so. Revision/first-seen history is unknown. Multileg or
ambiguous-side windows remain unknown, not quiet zero. Normalization reads only
strictly earlier same-time buckets; price features read completed earlier bars.
No original live eligibility, news, sector or gamma availability is reconstructed.
The reused historical period and three selected names do not validate the full
live strategy, and these negative proxies do not constitute a new verdict on
that distinct full policy. Survivorship, adjustment quality and actual executable
spread/fill costs remain limits. No live thresholds were changed.

The audit/display addendum's prose date says 2026-11-02; its actual authoring and
execution were 2026-11-01 UTC after first results. It claims no blind registration
of a new strategy. The source and original result hashes/timestamps govern.

## Review and reproduce

Private `/arastirma` shows the three base rows, all cost attributions, timing
control and scope limits. Original daily-bar research and prospective log-only
cohorts remain distinct. Integrity failure shows unavailable; HTTP requests run
no optimizer, vendor query, database mutation or promotion.

- Original report hash: `b3b411c1092f00df8f39eadb03aca4050cb4425ff10befd88b2b2dfb080d17af`.
- Audit hash: `546c385c34262e7905e945ee676bfa6c6ee5d66d574630a189ea4cd8c2f6fd16`.
- See `artifacts/vendor-history-v1/README.md` for independent re-audit command.
- Raw vendor payloads remain private outside git; only derived ledgers, source
  hashes and audit metadata ship.

Next admissible evidence is the already separate prospective observation stream
and enough genuinely available history for the existing support gates. A changed
hypothesis needs a new explicit protocol and an unseen evaluation window; repeated
reuse of these negatives must not be sold as fresh confirmation.
