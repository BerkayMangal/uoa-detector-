# Audit — H10 trade by trade, recomputed independently

**2026-09-23** · script `scripts/options_alpha_h10_trade_audit.py` · 0 UW requests

This is an audit, not a rescue. No threshold, arm definition, floor or exit rule was
changed. One implementation bug was found in the shared exit engine. It was fixed,
and all four families that used the engine were rerun **as separate v2 artifacts**.
The v1 artifacts stay in place, byte for byte.

---

## 1. How the ten were chosen (before any P&L was looked at)

Within each arm, records are ordered by `sha256(symbol|entry_session)` and the first
five are taken. The ordering does not depend on any outcome, and anyone can
reproduce the same ten.

**What is independent:** entry sides, the 2% slippage, commission, the daily closable
value, the time exit and net P&L. All of these are recomputed from the raw harvest
JSON with plain `json` and `Decimal`. The audit does not use `parse_chain_row`,
`price_structure` or `evaluate_exit`.

**What is not independent:** the leg identity (which two strikes). The result
artifact records only the anchor, so the audit rebuilds the legs with the engine's
own `build_candidate`. It reconstructs that choice; it does not re-decide it.

**Information time:** contract volume for session D is published on D+1. Entry is
the close of D+1. The hold is the next 5 sessions and the exit is the close of the
5th. Raw source for each day: `artifacts/options-alpha-v1/harvest/<session>/<TICKER>.json`.

## 2. The ten structures

Multiplier 100. Entry: long leg at the ask, short leg at the bid, then +2%. Exit:
long leg at the bid, short leg at the ask. Commission is $0.65 per leg per contract,
round trip.

| Arm | Structure | Legs at entry (bid/ask) | Information → entry | Exit | Qty | Debit raw → +2% | Comm. $ | Max loss $ | Independent | Net P&L $ | Engine v1 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | SPY bear put | L 750P 8.39/8.43 · S 749P 8.04/8.08 | D 07-02 → 07-06 | 07-13 | 2 | 0.39→0.40 | 5.20 | 85.20 | realised 0.34 | **−17.20** | −17.20 ✓ |
| A | AMZN bull call | L 270C 4.95/5.05 · S 272.5C 4.10/4.35 | D 08-21 → 08-24 | 08-31 | 1 | 0.95→0.97 | 2.60 | 99.60 | realised 0.42 | **−57.60** | −57.60 ✓ |
| A | AAPL bull call | L 320C 3.30/3.40 · S 322.5C 2.60/2.98 | D 08-05 → 08-06 | 08-13 | 1 | 0.80→0.82 | 2.60 | 84.60 | **UNKNOWN** (days 4–5 unpriced) | — | −67.60 ✗ |
| A | NVDA bull call | L 230C 2.34/2.72 · S 232.5C 1.85/2.19 | D 08-28 → 08-31 | 09-08 | 1 | 0.87→0.89 | 2.60 | 91.60 | realised 0.78 | **−13.60** | −13.60 ✓ |
| A | SPY bull call | L 750C 4.82/4.85 · S 751C 4.36/4.39 | D 07-01 → 07-02 | 07-10 | 1 | 0.49→0.50 | 2.60 | 52.60 | **UNKNOWN** (days 3–5 unpriced) | — | −2.60 ✗ |
| B | SPY bull call | L 779C 2.43/2.45 · S 780C 2.12/2.15 | D 09-03 → 09-04 | 09-14 | 2 | 0.33→0.34 | 5.20 | 73.20 | **UNKNOWN** (days 2–5 unpriced) | — | −39.20 ✗ |
| B | SPY bear put | L 760P 7.92/7.95 · S 759P 7.56/7.59 | D 08-31 → 09-01 | 09-09 | 2 | 0.39→0.40 | 5.20 | 85.20 | **UNKNOWN** (day 5 unpriced) | — | −33.20 ✗ |
| B | TSLA bull call | L 370C 5.30/5.40 · S 372.5C 4.70/4.80 | D 08-24 → 08-25 | 09-01 | 1 | 0.70→0.71 | 2.60 | 73.60 | realised 0.55 | **−18.60** | −18.60 ✓ |
| B | SPY bear put | L 754P 6.02/6.05 · S 753P 5.80/5.83 | D 08-28 → 08-31 | 09-08 | 3 | 0.25→0.26 | 7.80 | 85.80 | realised 0.20 | **−25.80** | −25.80 ✓ |
| B | QQQ bear put | L 685P 10.61/10.81 · S 684P 10.19/10.49 | D 07-30 → 07-31 | 08-07 | 1 | 0.62→0.63 | 2.60 | 65.60 | **UNKNOWN** (days 3–5 unpriced) | — | −92.60 ✗ |

- **Entry debit:** matches the engine 10 of 10.
- **Net P&L:** matches 5 of 10. The other 5 are not arithmetic differences. The
  engine booked a realised number where the true state is **unknown**.

The full per-day quotes for every trade are in
`artifacts/options-alpha-v1/h10_trade_audit_v2.json`.

## 3. Finding 1 — BUG: an unknown exit was booked as a realised one

**Mechanism.** The harvest keeps rows with **DTE 10–70** (`KEEP_MIN_DTE = 10` in
`scripts/options_alpha_harvest_chains.py`). The engine's forced close is at
**DTE 7** (`close_at_dte`). So days at DTE 7–9 cannot be seen. A position whose hold
crosses DTE 10 loses its quotes. When no rule fired, `evaluate_exit` fell through
and booked **the last priced day** as a `time` exit.

Example: SPY 779/780 call, entry 09-04. Only day 1 was priced (0.17). v1 booked
−39.20 $ as a "5-day time exit".

**Whole sample (73 records):** 21 were affected (A 13, B 8). Of the 52 records with
a priced 5th day, all 52 match the independent calculation to the cent.

**Fix** (`src/uoa_detector/options_alpha/exits.py`). When no rule fires, the engine
never borrows an earlier day's value as the exit:
- The horizon reached, but its last day is unpriced → `NO_EXIT_DATA`. The last
  priced day goes into a note and is marked "KULLANILMADI" (not used).
- Fewer observations than the horizon → `STILL_OPEN`. This matters for the live
  PAPER tracker, which scores positions mid-hold.

Regression tests: `test_an_unpriced_last_horizon_day_is_unknown_not_an_earlier_day`
and `test_a_position_short_of_its_horizon_is_still_open`.

Two existing tests fed 2 observations into a 5-day hold, and one of them expected a
`time` exit. That expectation encoded the bug. Both now supply 5 days, and their
assertions (unpriced day counted, never zero; no priced day means no outcome) are
unchanged (D10: the test was wrong, documented here).

**Reproducibility check.** With the old engine, the H10 runner reproduces the
committed `h10_result.json` records **exactly** (73/73). So the whole v1→v2
difference comes from this fix.

## 4. Finding 2 — model property, NOT changed: the closable value can be negative

Exit prices a vertical leg by leg: long at the bid, short at the ask. On a narrow
spread with wide quotes that sum can go below zero, and the booked loss then exceeds
the debit paid. Across the sample, 16 records saw a negative value on some day.
**6 realised records lose more than their max loss.**

This is the frozen conservative cost convention, and it was not changed. Choosing a
friendlier exit policy after seeing results would be a threshold search. Sensitivity
only, with loss capped at max risk (i.e. "hold, do not pay to close"):

| Arm | Realised | Unknown | Mean (primary) | Mean (loss capped) |
|---|---|---|---|---|
| A cheap | 32 | 13 | −53.79 $ | −48.94 $ |
| B rich | 20 | 8 | −46.13 $ | −26.63 $ |

The cap helps B more than A, so the direction against the hypothesis does not flip.

## 5. Effect on every family that used the engine (v2 artifacts)

| Family | v1 verdict | v2 verdict | Arms with P&L v1 → v2 | Mean A / B v1 → v2 |
|---|---|---|---|---|
| H03 | REJECTED | **REJECTED** | 134/7 → 96/4 | −75.31 / −52.57 → −83.49 / −37.15 |
| H01 | REJECTED | **REJECTED** | 111/91 → 80/78 | −54.52 / −70.75 → −55.18 / −75.77 |
| H04 | REJECTED | **INSUFFICIENT_DATA** | 42/33 → 31/23 | −60.47 / −51.93 → −58.68 / −53.06 |
| H10 | INSUFFICIENT_DATA | **INSUFFICIENT_DATA** | 45/28 → 32/20 | −49.33 / −49.78 → −53.79 / −46.13 |

- **H04:** once the unknowns are removed, arm B has 23 completed structures, below
  the floor of 30. The verdict label changes. The direction does not: A still trails
  B, and every arm is negative after costs. INSUFFICIENT_DATA is the honest label.
  It is **not** a softer rejection that invites a rerun with a looser floor.
- **H10:** v1's +0.45 $ "A beats B" becomes −7.66 $. A's only winning trade was an
  unknown. The negative result stands and is, if anything, stronger.
- No family moved toward a pass. **No alpha was found and none was recovered.**

Artifacts: `h0{1,3,4}_result_v2.json`, `h10_result_v2.json`,
`h10_trade_audit.json` (against v1), `h10_trade_audit_v2.json` (against v2).
The v1 files are untouched.

## 6. What remains unknown, and the one way to know it

The 21 H10 unknowns (and their counterparts in H01/H03/H04) are unknown because the
harvest threw those rows away, **not** because the market had no quotes. The rows
can be fetched: the per-contract daily NBBO history endpoint is VERIFIED in
`capability_matrix.json`. That is roughly 2 legs × affected records, a few hundred
requests at most against a 5,000/day safe ceiling. Until that fetch runs, these
outcomes stay **UNKNOWN**. They are not losses, and they are not zero.
