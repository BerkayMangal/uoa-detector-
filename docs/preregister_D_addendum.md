# Pre-Registration Addendum — Study D: causal metric is the decision metric

**Committed BEFORE the fresh-window run.** This addendum refines the verdict rule of
`docs/preregister_D.md` (commit `2e3094a`). Per CLAUDE.md D1, the frozen pre-reg is
**not edited**; this new doc records the refinement and is committed before any number
from the fresh 2024 window is produced.

## Why this addendum exists

The instrument check on the burned panel (commit `7e9a0f6`, IN-SAMPLE, not the verdict)
established a fact the original §6 did not fully price in:

- The **inherited** `iv_pct` ranks each day's IV against the **full window** (including
  future days). This is an in-window **look-ahead**. The pre-reg §2 flagged it but kept
  it as the **primary** statistic on the grounds of "inherit the conditioning exactly."
- On the burned panel, stripping that look-ahead (causal trailing IV-percentile) shrank
  the VRP Sharpe-spread **+0.80 → +0.51** and the cond-vs-uncond Welch-t **+2.06 → +1.88**
  (below 2). **A material slice of the headline spread is look-ahead, not signal.**

A replication test whose decision metric still contains look-ahead can "survive" on the
artifact rather than the signal. That defeats the purpose of D. Therefore:

## The refinement (binding on the fresh-window run)

1. **PRIMARY (decision) metric = the CAUSAL / trailing-IV-percentile variant.** The
   verdict is read off the causal arm's numbers — `SPREAD_causal = Sharpe(COND_causal)
   − Sharpe(UNCOND)`, its `t_cond`, and its DSR (`n_trials=1`).

2. **Pre-committed verdict thresholds (pre-reg §6 table), applied to the CAUSAL arm:**

   | Outcome | Condition (on the **causal** arm) | Action |
   |---|---|---|
   | **DEAD** | `SPREAD_causal ≤ 0` | Conditioning was a burned-panel artifact. **D dead. B and C dead-on-arrival** — not started. Done. |
   | **WEAK** | `SPREAD_causal > 0` **but** (`\|t_cond\| < 2` **or** `DSR ≤ 0.95`) | Not robustly replicated, look-ahead-free. **No green light** to B/C. Decision returns to Berkay. |
   | **SURVIVED** | `SPREAD_causal > 0` **and** `\|t_cond\| ≥ 2` **and** `DSR > 0.95` | Look-ahead-free information replicated. **STOP — design C together.** Do not start C. |

   Welch-t (cond − uncond) of the causal arm is reported alongside as corroboration; the
   gate condition is `t_cond` and DSR, per the inherited verdict machinery.

3. **The inherited look-ahead `iv_pct` variant is REFERENCE-ONLY** — reported for
   comparison with the documented in-sample numbers, **never** the decision metric.
   The in-sample +0.80 / fresh-window inherited spread is **not** a kill-or-pass input.

## Everything else in `preregister_D.md` stands unchanged

Window (2024-05-01 → 2025-04-30), 24-ticker universe, conditioning definition,
baseline, VRP statistic, single run, no re-tuning, no post-hoc window/ticker/threshold
changes. This addendum changes **only which of the two already-pre-registered variants
carries the verdict** — from the inherited look-ahead variant to the causal variant.
No new researcher degree of freedom is introduced; if anything, the freedom to be
flattered by look-ahead is removed.
