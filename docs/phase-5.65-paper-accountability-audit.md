# Phase5.65 — actual PAPER accounting audit,2026-11-06

The15:55 London private snapshot contains98 Live Alpha SOURCE records, all
stock/up.82 priced closes have23 wins/59 losses and net **-2880.55USD**. Separate
states:1 pending,6 open,1 exit-price pending,1 unfilled,7 unknown. Those16
unpriced records are excluded from priced totals, not assigned zero losses.
The home terminal display selects20 recent records:19 priced losses,1 unfilled.
It therefore shows no winner although full history contains23. These are not
CALL/PUT premium returns. NFLX remains exit-price pending, not a priced loss.

## Independent event reconciliation

Fetched the existing private history route for each of82 priced opportunities,
with concurrency2 and no vendor HTTP, DB mutation, price repair or new fill.
Each has one matching paper_fill/paper_exit pair for the same paper_id. Using
Decimal arithmetic, `(exit_price-entry_price)*shares` matches each displayed
and event PnL to its recorded cents. **82 MATCH,0 mismatch,0 unverified**.
Sum of per-trade rounded values:-2880.55; unrounded sum:-2880.5673. Small
aggregation differences are rounding, not missing trades. This reconciles
recorded accounting; it does not validate executable fills or raw price truth.

The actual exit categories explain the loss:19 stops net-2424.82;56 opposite-flow
thesis exits net-807.22;5 time exits net-42.73;2 target exits net+394.22. Distinct
source opportunity days22;82 overlapping records are not82 independent sessions.

## Fixed-clock inverse diagnostic

The separately registered diagnostic keeps all82 original entry/exit clocks
and original share quantities. Frozen modeled stock slippage is5bp per side.
Infer entry reference `S0=entry/(1+s)` and exit reference `S1=exit/(1-s)` from
the original rounded fills, with `s=.0005`:

```
original = q * (S1*(1-s) - S0*(1+s))
mirror   = q * (S0*(1-s) - S1*(1+s))
mirror   = -original - 2*q*s*(S0+S1)
```

Underlying gross:-2757.41447USD; modeled slippage:123.15283USD. Fixed-clock
mirror net **+2634.26165USD**,55 positive/27 negative records, before borrowing,
locate, dividend, margin and execution-availability effects. Aggregate rounding
uncertainty from the recorded four-decimal fills is bounded by0.05201USD under
the stated model. Reversing the already-net PnL sign would overstate the result.

This is a post-hoc reuse of a known losing sample. Keeping the original exits
does not test a reversed stop/target policy. No out-of-sample result, capital-
constrained portfolio, executable short-stock fill or live recommendation is
created. Buying a PUT instead of a CALL is not the arithmetic negative of the
CALL premium return. No existing live rule or PAPER record is inverted.

Private rowwise reconciliation SHA256:
`4ac8b2aebe4fa2eab2c8e41ea0dc7990ad71d54d3f718c3d0997feb6041b3fba`.
Raw private HTML, immutable-event extraction and rowwise mirror arithmetic stay
outside git under the session's paper65-audit evidence directory.

## Actual display defects and separate old option assumptions

Home previously exposed raw last_mark without last_mark_at or freshness even
when the positions screen correctly showed an unknown current mark. Actual
IBIT/NFLX examples had November4/October30 samples. Boolean fallback also hid
legitimate zero-valued option marks. The terminal slice called unfilled records
closed and obscured the recent-versus-full denominator. The scoped display fix
exposes quote/fill clocks and categories without modifying any financial record.

Current stock/long-option PnL signs, contract multipliers, modeled fees and
missingness guards passed the separate188-case focused regression audit.

Separate `/opsiyon` shows four old September replay spreads, all losses, net
-136.00USD. Independent calculations reproduce their recorded results:
SPY09/22:(.29-.58)*100-2.60=-31.60;
QQQ09/22:(.24-.48)*100-2.60=-26.60;
SPY09/08:(.34-.62)*100-2.60=-30.60;
QQQ09/08:(.20-.41)*100*2-5.20=-47.20.
The-79.80 headline is a different pair of committed time-only card outcomes,
not the sum of the four tracker rows.

Legacy EOD stop logic assumes an exact threshold fill rather than the observed
closing liquidation value. For QQQ09/08 it uses.20 instead of observed.13,
understating that modeled loss by14USD; this cannot explain artificial losses.
The tracker also displays later marks beside an earlier exit result. These
frozen replay assumptions need a separate corrected measurement protocol;
historical artifacts, raw marks, old verdicts and live totals are preserved.

Publication validation and exact release proof are recorded in the closeout/PR.
