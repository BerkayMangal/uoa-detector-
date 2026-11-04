# Phase 5.54 actual results — no selected winner

The registered 1,440 reused-history sensitivity cells were computed. **368 have
complete reference-return coverage; all 368 have negative net dollars after
their specified costs.** The remaining 1,072 are partial, not zero-return cells;
none is completely blocked. There are 30,720 deduplicated signal/delay/horizon/
cost outcomes across the cells, not 30,720 independent trades. Twelve cells
repeat the original three-arm/four-cost controls and reconcile to their entire
published ledgers. No live rule, original call or PAPER record changed.
Deduplication removes repeated candle-filter outcomes within an arm; the same
ticker/time can still recur in different original arms and is not independent.
These historical proxy rosters do not reproduce the current complete live
news/relative-strength/opening policy or the owner's later NFLX trade.

## Source and timing

The checked archive spans June 24–September 18, 2026: 61 source sessions, 20
warmup and 41 evaluation sessions. The fixed rosters contain 85 baseline,
94 normalized and 77 normalized-price signals. All 427 minute/flow packet
identities and checksums were verified; 244 minute packets yield 95,160 valid
RTH OHLC candles. Sixteen negative-volume fields are preserved and disclosed;
they cannot support a clean volume/VWAP or executable-liquidity claim.
This phase did not open the four mixed-date daily packets, chains or Study G.

Entry is the first RTH minute open strictly after the registered delay, within
five minutes. Aligned signals therefore enter at +2/+6/+16 minutes for the
requested 1/5/15 delays. Intraday exits use the exact minute open strictly before
RTH close. Dated closes use the final regular minute close, not the 16:00
extended-hours open. Historical availability is still a vendor event-time
assumption, not physically observed point-in-time evidence.

## Same-signal primary panel

The calendar-only rule retains 66 of 85 baseline signals over 36 common days.
The five final entry dates, September 14–18, cannot have complete +5-session
endpoints inside the frozen source and are excluded from this panel, not inserted
as idle zero days. Late-day signals are excluded by the registered worst-case
delay/tolerance/180-minute feasibility rule. These exclusions do not use returns.

The table uses no extra candle gate, delay 1 and 5 bp **per side**. Figures are
sums of independent whole-share $2,500 reference units; SPY return is applied to
the same debit. They are not the return of a $2,500 account or a capital-constrained
portfolio. Multi-session units overlap and use more capital-time.

| Endpoint | Net reference $ | Matched-SPY excess $ |
|---|---:|---:|
| 5 minutes | -199.77 | -41.23 |
| 15 minutes | -182.90 | -13.78 |
| 30 minutes | -195.50 | -9.19 |
| 60 minutes | -326.65 | -133.60 |
| 120 minutes | -321.85 | -142.15 |
| 180 minutes | -206.29 | -34.18 |
| Same-session close | -385.49 | -128.18 |
| +1 session close | -344.57 | -144.86 |
| +3 session close | +519.90 | +433.88 |
| +5 session close | +978.11 | +536.21 |

The positive +3/+5 common-panel numbers do **not** override the negative complete
full-roster cells. Full-roster long endpoints remain partial/right-censored.
All non-anchor descriptive paired-difference bands include zero. For +5 versus
60 minutes, the mean daily-cohort excess difference is +$18.61 and the shared
descriptive band is [-$28.48, +$65.69]. This is neither a calibrated confidence
claim nor a p-value: the sample was already inspected, and five-session blocks
cannot guarantee all dependence from overlapping +5-session exposures is covered.

## Post-hoc concentration/risk worksheet

`description.json` explicitly labels these 40 fixed-panel views as **post-hoc
descriptive concentration, not new validation**. It summarizes every registered
endpoint/cost on the same 66 signals. Removing winners never reallocates a slot
or cash, and best-net and best-excess deletions use separate ranking lists.

At +3 sessions/5 bp, removing the best three net wins changes +$519.90 to
**-$156.39**. At +5 sessions/5 bp, +$978.11 becomes **+$14.72**; independently
removing the best three excess wins changes +$536.21 SPY excess to **-$116.83**.
Thus the positive multi-session observation is concentrated and unvalidated.

For +5 sessions, net at 5/10/20/50 bp per side is respectively
+$978.11 / +$820.37 / +$503.86 / -$411.66. After removing the best three net wins,
the 10/20/50 bp figures are -$135.34 / -$436.48 / -$1,305.93. The 5 bp result
comes from AAPL -$289.34, NFLX +$247.89 and NVDA +$1,019.55; three selected names
do not establish a broad-universe effect. No losing name is deleted or retuned.

All 66 primary paths are observed at each endpoint. Mean gross underlying MAE
is -0.33% at 15 minutes, -0.58% at 60 minutes and -3.35% at +5 sessions; worst
observed +5 MAE is -11.79%. These are continuous-path price diagnostics, not
account drawdown, a guaranteed stop fill or a policy with an optimized stop.
No option NBBO return, fundamental valuation or historical news effect is inferred.

## Audit and reproducibility

The independent auditor rederived **all marks, completed candle gates and MFE/MAE
paths**, unit arithmetic, all 1,440 cell aggregates, all 12 old controls, the
common day vectors and descriptive bootstrap. It uses the integrity source
adapter but none of the engine's pricing/filter/grouping/resampling functions.

The first audit correctly refused publication on a shared-band check: its
expectation applied the nonzero family radius to the anchor self-comparison.
The anchor difference is identically zero. The auditor was corrected to demand
exactly [0,0] for that anchor; non-anchor checks and tolerances stayed strict.
A 36-day nonzero-bootstrap fixture now tests this and rejects even tiny nonzero
anchor bands. The actual study was **not changed or rerun to improve results**.

From the repository root, with the private sealed archive already available:

```bash
uv run python -m scripts.horizon_research --source /workspace/uoa-history51-jun24 --output /tmp/horizon-new
uv run python -m scripts.audit_horizon_research --report /tmp/horizon-new/report.json --source /workspace/uoa-history51-jun24 --output /tmp/horizon-new/audit.json
uv run python -m scripts.describe_horizon_panel --report /tmp/horizon-new/report.json --output /tmp/horizon-new/description.json
```

Outputs refuse overwrite. No network/provider call is made. Source paths stay
outside git; only derived evidence is committed.

Pinned identities:

- Report: `ed405e06de5069070f746a9675b015ae9f6dd2620ee0add3a65b1c8380e8182e`
- Source: `773c63a92fc2933a2e73d20d9ab0dd90eed1927d92c020ba5e5e52860b5989b6`
- Profile: `cb54fb838e7885c8100157c056554df41f66f42ba294905a86edf5d773dfb771`
- Engine/protocol: `a3fb365208f78207860f41ba25b5552549f8a9794efd1f91b813f27cc164f174`
- Independent audit: `fd0cb497b2ce44275c547f68f7e4cfccffe060361fbaaef13a0b041ee2760044`
- Final auditor script: `54f8d1cb09d5452db2bdd02c8f4601747e9884ff361cc9e5080b338202c1598c`
- Post-hoc description: `34f424bd527ca4b05943c0e58f0d9126f179d267e7797e4879128f48a1b7b170`

Next evidence is genuinely subsequent, physically timestamped news/price capture
and fixed-arm PAPER evaluation. Existing minimum-session/support gates stay
unchanged. Wider retrospective searches cannot turn this reused sample into
independent validation; no candidate is promoted by this delivery.
