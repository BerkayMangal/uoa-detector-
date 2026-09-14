# Candidate Hypotheses — Map Only (NO study started)

**Status: MAP. No code written, no study run, no data pulled.** This document
exists to decide *together* which hypothesis to test next, on which window, against
which baseline. Nothing here is started.

## Why this document exists

The vol-premium edge is dead after costs — that verdict is closed
([`docs/edge_to_money.md`](edge_to_money.md): OOS Sharpe −0.56, Bonferroni ×30
p=1.00, Deflated Sharpe 0.13, −20% gap ≈ 5× credit). The **one** thing that
survived the gauntlet is informational, not tradeable: the *conditioning*
(long-gamma + high-IV-rank) beats blind vol-selling by **+0.77 Sharpe**. The
conditioning carries information about *which* ticker-days are better-than-average.

That is the only live thread. The candidates below all probe that thread from a
different structural angle. They are **not** tweaks of the dead study.

### Two hard rules these candidates obey

1. **The existing 11-month panel is burned.** It has been sliced ~30 ways; any
   new result on it is p-hacking by construction (the trap the project has
   avoided since day one — CLAUDE.md D4). Every candidate below **requires a new,
   non-overlapping data window**. Re-using `data/chain_snapshots` /
   `data/spot_series` from Jul-2025–Apr-2026 disqualifies the study before it
   starts.
2. **No candidate is a naked short-vol trade.** The dead edge died on the
   un-hedged, un-sampled tail. Each candidate below has a *structural* defense
   against that tail, stated explicitly. If a candidate cannot state how it
   avoids the naked-vol trap, it does not belong here.

### Discipline every candidate must satisfy (non-negotiable, applies to all)

These are pre-committed for all three. A candidate that skips any of them is
rejected regardless of result:

- **New data window** — a fresh, non-overlapping period. Not the existing panel.
- **Its own FDR budget** — declare the number of hypotheses/variants *before*
  running; Benjamini–Hochberg + Deflated Sharpe over that count. No post-hoc
  variant-adding.
- **Walk-forward OOS** — train/select on the earlier portion, test net on the
  held-out later portion. The headline number is the OOS number, never full-sample.
- **Real option costs** — bid/ask spread (the wide-spread names these signals
  select), slippage, and per-leg commission. P&L is net, sold at the bid.
- **Look-ahead audit + tail stress** — event-time only (CLAUDE.md D9); explicit
  simulated crash/gap stress because no fixed window is guaranteed to contain one.
- **A written kill number** — the figure at which the hypothesis is declared dead,
  fixed before seeing results.

---

## Candidate C — GEX regime → underlying next-day realized-vol / momentum

*(dealer-gamma **mechanics**, not direction-following)*

**The single question to test (formulate only — do NOT answer here):**
> Does the dealer-gamma regime on day *t* predict the underlying's day *t+1*
> behaviour — specifically, does a **short-gamma** regime (dealers amplify) precede
> *higher* realized volatility / momentum-continuation, and a **long-gamma** regime
> (dealers suppress) precede *lower* realized volatility / mean-reversion / pinning?

This is the dealer-hedging *mechanism* (short-gamma dealers chase price, long-gamma
dealers fade it), measured as a **realized-vol / autocorrelation forecast**, not a
directional call.

**Data needed — available vs missing:**
- Net GEX per ticker-day: **available** — `webapp/gamma.py:compute_gamma`
  (offline from a chain snapshot) and
  `src/uoa_detector/sources/thetadata_derived/dealer_gamma.py:ThetaDataDealerPositioningProvider`
  (`_net_gamma`, `_flip_strike`); live via
  `src/uoa_detector/sources/unusual_whales/providers/dealer_gamma.py:UnusualWhalesDealerGammaProvider.net_gamma_at`.
- Underlying next-day returns / realized vol: **available** plumbing
  (`src/uoa_detector/sources/unusual_whales/providers/price_action.py:get_intraday_price_movement`;
  daily close logic mirrors `webapp/gamma.py:_daily_close`).
- The new window's chain snapshots + spot series: **MISSING** — must be downloaded
  fresh (`scripts/download_chain_snapshots.py`, `scripts/compute_spot_series.py`).
  This is the gating cost.

**Why it dodges the naked-vol trap (structural defense):**
The natural trade expression is **buying** convexity when the regime predicts
amplified moves (long a straddle / long gamma in a short-gamma regime), or a
defined-risk directional play — i.e. a **long-tail / bounded-loss** position. It is
the *opposite* sign of naked short-vol: you are paying premium to own the move the
mechanism predicts, not collecting it. The catastrophic-gap tail that killed the
vol-premium edge is, here, the payoff, not the risk.

**How we know if it's dead (pre-committed kill numbers):**
- Regime → next-day realized-vol rank correlation (IC) on the OOS window **< 0.05**
  (or sign-flips vs the in-sample IC) → dead. This is the exact failure mode that
  already killed `study_gamma_regime` (LCID-squeeze artifact, non-overlap t=0.94)
  and `study_gamma_pinning` (corr ~0, sign-flips) — so the bar is: must hold on a
  *fresh* non-overlapping window with a *stable sign across sub-periods*.
- Any tradeable expression with OOS Sharpe **< 0** after real costs → dead.
- Fails its FDR budget (BH-adjusted p ≥ 0.05, or DSR < 0.95) → dead.

---

## Candidate B — Index-vs-single-name VRP spread

*(sell richer single-name vol, hedge the systematic tail with index vol)*

**The single question to test (formulate only — do NOT answer here):**
> After costs and after hedging out the systematic component, is the **single-name
> minus index** variance-risk-premium spread positive and stable — i.e. does
> single-name implied richness, net of the index VRP you pay to hedge, leave a
> residual premium that survives the gauntlet?

The dead study sold *gross* single-name vol. This sells single-name vol **relative
to** index vol, isolating the idiosyncratic VRP and explicitly paying away the
systematic VRP as a hedge.

**Data needed — available vs missing:**
- Single-name IV / IV-rank: **available** —
  `src/uoa_detector/sources/unusual_whales/providers/iv_history.py:UnusualWhalesIVHistoryProvider.iv_rank_at`,
  `webapp/gamma.py:atm_iv`.
- Index vol leg: **partially missing.** SPY/QQQ are ordinary tickers, so an
  **ETF-proxy** index leg is reachable through the same per-ticker IV + chain pulls.
  A true **cash index (SPX/VIX-style)** leg is **MISSING** — there is no
  index-option ingestion path in the repo today (the chain/snapshot readers target
  single-name equity options:
  `src/uoa_detector/sources/thetadata_derived/chain.py`,
  `.../snapshot_reader.py`). Decide up front: SPY-proxy (cheaper, basis risk) vs
  building SPX ingestion (cleaner, real work).
- Realized vol for both legs over a fresh window: **MISSING** — new download.

**Why it dodges the naked-vol trap (structural defense):**
The long index-vol leg is a direct hedge against the exact event that made the dead
edge un-survivable: a market-wide crash that hits *every* short single-name straddle
at once. When the −20% gap comes, the long index vol pays off and caps the book-level
loss. The position is **relative-value / market-neutral-in-vega**, not an outright
short-vol bet — so the un-sampled systematic tail is structurally bounded, not
ignored.

**How we know if it's dead (pre-committed kill numbers):**
- OOS spread Sharpe (net of *both* legs' costs and the hedge cost) **< 0** → dead.
- Hedge ratio unstable across sub-periods (the index beta that neutralizes the tail
  swings enough that the "hedge" doesn't hedge in the OOS window) → dead.
- Residual single-name-minus-index premium not significant after the FDR budget
  (BH p ≥ 0.05 or DSR < 0.95) → dead.
- Tail stress: a −20% systematic gap leaves net book loss **> 1× collected credit**
  after the index hedge pays → the hedge failed its one job → dead.

---

## Candidate D — Replication of the conditioning signal on a fresh window

*(test the ONE survivor as pure information, before any trade)*

**The single question to test (formulate only — do NOT answer here):**
> On a fresh, non-overlapping window, does the conditioning (long-gamma +
> high-IV-rank) still select better-than-baseline ticker-days **as measured in
> realized-minus-implied vol points** — does the +0.77-Sharpe-vs-blind information
> content *replicate*, or was it in-sample luck of the original 11 months?

This is the cheapest, highest-value next step: it falsifies or confirms the *only*
thread that survived, **without taking any vol position at all.** If the
information doesn't replicate, candidates B and C lose their premise and we stop.

**Data needed — available vs missing:**
- Conditioning inputs (GEX regime + IV-rank): **available** (same functions as
  Candidate C: `webapp/gamma.py:compute_gamma`, `:atm_iv`;
  `unusual_whales/providers/iv_history.py:iv_rank_at`).
- Forward realized-vs-implied vol per ticker-day: **available logic** (the
  realized/terminal-move computation mirrors `webapp/gamma.py` /
  `scripts/edge_validation.py:_build_panel`, but **must run on new snapshots**).
- Fresh window snapshots: **MISSING** — new download (same gating cost as C).

**Why it dodges the naked-vol trap (structural defense):**
It takes **no position** — it is a measurement of information content (rank-IC /
Sharpe-spread of conditioned vs unconditioned, in vol points). There is no traded
short-vol leg, therefore no tail to be blind to. Only if the information replicates
do we proceed to a *hedged* expression (B or C); we never trade D directly.

**How we know if it's dead (pre-committed kill numbers):**
- Conditioned-minus-unconditioned Sharpe-spread on the new window **≤ 0** (the
  +0.77 was luck) → dead, and B/C are abandoned with it.
- The spread is positive but does not survive its FDR budget → treat as not
  replicated → dead.
- The conditioning's information lives *entirely* in costs/wide-spread names again
  (i.e. it is informational in vol points but cannot exceed even the hedge cost in
  any expression) → confirms "context only," and we keep the screener exactly as
  built, mechanical trading stays off the table.

---

## Decision needed (do NOT proceed without it)

Open questions to settle together before *any* of these starts:

1. **Which candidate first?** D is the cheapest falsification of the shared premise
   and arguably gates B and C. C tests the cleanest mechanism. B is the most work
   (index ingestion) but the most structurally robust if it survives.
2. **Which fresh window?** Length, tickers, and the source (ThetaData download vs
   live UW capture). This determines the FDR budget and the cost/benefit of the
   download.
3. **Which baseline** each runs against (blind short-vol, random-day, buy-and-hold
   convexity).

**Nothing here is started. The map ends here.**
