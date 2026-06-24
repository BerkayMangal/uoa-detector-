# Edge → Money — Phase 1 Gate Report

**Mandate:** turn the vol-premium edge into real money. Phase 1 was the gate —
prove the edge is *tradeable* (survives realistic costs + tail + multiple
testing + walk-forward), and only then build the live/speed/forward work.

**Verdict: THE EDGE DOES NOT SURVIVE THE GATE. It is a statistical artifact, not
tradeable money. Per the mandate, Phases 2–4 are NOT started.**

This is not hedging. The gross statistical signal is real-ish (VRP in vol points,
non-overlap t=2.64). But money is net of costs, robust out-of-sample, and
survivable in the tail — and on every one of those three axes the edge fails.

Reproduce: `PYTHONPATH=src:. .venv/bin/python scripts/edge_validation.py`
(local data only — `data/chain_snapshots` + `data/spot_series`, no live API).

---

## What was tested

The claimed edge: **sell vol when dealers are long-gamma AND IV-rank ≥ 75%**
(options are structurally overpriced → collect the premium). The earlier studies
measured this in *vol points* (realised < implied). That is not P&L. Phase 1
rebuilds it as the actual trade a retail seller would put on and runs it through
the gauntlet that decides whether to risk capital.

- **Tradeable structures, BS-priced, held to ~1-month expiry, unhedged:**
  - *Short straddle* — the raw edge, unbounded risk.
  - *Iron butterfly* — the defined-risk version (the UI-recommended structure),
    wings at 1.5× the expected 1-month move; gives a capital base for Sharpe.
- **Realistic costs:** you SELL at the bid. Option bid/ask spread modelled at
  5% / 10% / 20% (the high-IV-rank names this signal selects are the *wide-spread*
  ones — 10–20% is representative, 5% is generous) + $0.65/leg commission.
- **Tail stress:** a −20% overnight gap during the hold. The sample (Jul 2025–
  Apr 2026, n=63 non-overlapping signals) contained **no vol crash**, so the tail
  was never observed — it must be simulated.
- **Baselines:** unconditioned short-vol (sell on every ticker-day) — does the
  signal beat blind vol-selling?
- **Walk-forward OOS:** train on the first 60% of dates, test net-of-cost on the
  last 40%.
- **Multiple testing:** Bonferroni ×30 (≈30 hypotheses tested across all studies)
  + Deflated Sharpe Ratio (Bailey & López de Prado — penalises for trials, skew,
  kurtosis, sample length).

Panel: 5342 ticker-days; 1082 raw SELL signals → **63 non-overlapping** trades
(the honest n, after removing the 20-day window overlap that inflates t-stats).

---

## Results

### 1. After realistic costs, the edge is insignificant

| Option spread | Short straddle (Sharpe, t) | Iron butterfly (Sharpe, t) |
|---|---|---|
| 5% (generous) | +0.96, **t=2.14** | +0.72, t=1.62 |
| **10% (representative)** | +0.68, **t=1.51** | +0.56% mean ret, **t=0.91** |
| 20% (meme/wide) | +0.12, t=0.26 | **−2.9% mean ret, t=−0.51** |

The edge lives *entirely* in the cost assumption. At a generous 5% spread the raw
straddle clears t=2 — but the high-IV-rank names this signal fires on are exactly
the wide-spread names (10–20%). At a representative 10% spread the defined-risk
trade is t=0.91 (insignificant); at 20% it is **negative**. There is no spread
level where the *defined-risk, tradeable* version is significant.

### 2. It loses money out-of-sample

Walk-forward, test window = last 40% of dates (after 2025-11-18):

> **Iron butterfly OOS Sharpe = −0.56 (t=−0.89), mean −8.5% on capital, n=32.**

Trained on the first 60%, the signal **loses money** in the held-out period. This
alone disqualifies it — an edge that doesn't survive a simple in-sample/out-of-
sample split is not an edge.

### 3. It does not survive multiple-testing correction

- Butterfly net-of-cost t=0.91 → raw p=0.36.
- **Bonferroni ×30: p=1.00** — does not survive at 0.05.
- **Deflated Sharpe Ratio = 0.13** (need > 0.95) — does not survive.

Across ~30 hypotheses tested in this project, a t=0.91 result is exactly what you
expect from noise. The DSR — which also penalises the −14 skew and fat tail —
puts the probability the Sharpe is real at 13%.

### 4. The tail is catastrophic and was never sampled

A single −20% overnight gap on the SELL book:

- **Short straddle:** collects ~$1,701/contract of credit; the gap turns that into
  a **mean −$1,616, worst −$9,169 per contract**. One gap erases the credit and
  then multiples of it. Unbounded — this is the un-hedgeable version.
- **Iron butterfly:** loss is *capped* (mean −$1,987, worst −$8,326/contract) —
  the defined-risk structure does its job. But the cap is still ~5× a typical
  month's credit, and the 11-month sample contained zero such events, so the
  realised Sharpe above never paid for this risk.

Short-vol's whole danger is that it looks great until the one day it doesn't. n=63
with no crash means the backtest is blind to the only outcome that matters for
sizing.

### 5. The one genuinely positive finding

> Unconditioned short-vol (sell on everything): Sharpe **−0.37** (t=−1.72).
> Conditioned on the SELL signal: Sharpe **+0.41** (t=+0.91).
> **The signal beats blind vol-selling by +0.77 Sharpe.**

The conditioning (long-gamma + high-IV) carries *information* — it reliably picks
the better-than-average ticker-days to sell vol. That is real and worth keeping as
**context in the discretionary screener**. But "better than a losing baseline" is
not "a profitable strategy after costs." +0.41 Sharpe, insignificant, negative
OOS, fails FDR — you cannot mechanically trade it.

---

## Honest verdict

| Gate criterion | Result |
|---|---|
| Significant after realistic (10%) costs? | **No** (defined-risk t=0.91) |
| Positive out-of-sample (walk-forward)? | **No** (−0.56 Sharpe, loses money) |
| Survives FDR (Bonferroni ×30)? | **No** (p=1.00) |
| Survives Deflated Sharpe Ratio? | **No** (0.13) |
| Tail survivable / sampled? | **No** (no crash in 63 obs; one −20% gap ≈ 5× credit) |
| Beats unconditioned baseline? | Yes (+0.77 Sharpe) — informational only |

**The vol-premium edge is statistically suggestive but NOT tradeable.** The clean
t=2.64 in vol-points collapses the moment you charge realistic option spreads,
hold out a test window, correct for the ~30 hypotheses searched, and price the
tail. This is the exact gap the mandate set out to test — *statistical signal vs.
money* — and the answer is: it's signal, not money.

This is consistent with everything the project already found (no mechanical
directional edge; gamma direction and pinning both died on non-overlap; the H1
vol effect died at t=1.08). The vol-premium was the last survivor, and held up
*as a vol-point regularity*. It does not survive as a *cost-and-tail-aware
dollar strategy*.

## What this means for the project

1. **Do not deploy a mechanical vol-selling strategy.** No size survives — not
   via fractional Kelly (the edge estimate is indistinguishable from zero and the
   tail is unbounded/un-sampled), not at any spread the real names trade at.
2. **Keep the screener as decision-support, exactly as built.** The IV-rank +
   gamma-regime read is informational (+0.77 Sharpe vs. blind) — useful *context*
   for Berkay's discretionary judgment, never an auto-signal. `webapp/gamma.py`
   already says this; it remains correct.
3. **Phases 2–4 are not started** (live=backtest wiring, speed/cost, forward
   harness). Per the mandate: "Edge FAZ 1'de ölürse FAZ 2-4'e girme, sadece
   raporla." There is no point optimising the latency and $/scan of a signal that
   has no net edge to harvest.
4. **The honest path to money is not in this signal.** Options: (a) accept the
   detector is a research/screening tool, not an alpha engine; (b) test a
   *different* hypothesis class (the conditioning carries info — a longer/cleaner
   sample, an earnings-aware filter, or an index-vs-single-name VRP spread *might*
   survive, but that is a new study with its own FDR budget, not a tweak of this
   one — and re-slicing this same 11-month panel until something passes is the
   p-hacking trap the project has avoided from day one).

---

*Method notes: r=0 Black-Scholes; ATM straddle = 2×call(K=S); butterfly payout =
min(|S_exit−S_entry|, W); non-overlap = every 20th obs per ticker; annualisation
√(252/20). DSR via Bailey–López de Prado with Acklam inverse-normal. All figures
from `scripts/edge_validation.py` on local snapshot data, reproducible and
bit-stable across runs.*
