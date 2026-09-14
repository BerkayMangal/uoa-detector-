# Design — Vol-Premium Edge Dashboard (landing)

**Status:** design, pre-implementation. Brainstormed + approved 2026-06-26.
Feeds an implementation plan (writing-plans), not built yet.

## 1. Goal

Add a top-of-page **landing dashboard** to the live screener that points the
user's attention at the **highest-probability honest edge** in the system and
makes the next action clear — the user currently looks at the flow cards and
does not know what to do.

The highest-probability edge per the project's own validation is the
**vol-risk premium** (selling vol): on the fresh 2024 window the *unconditioned*
premium replicated strongly (Sharpe +1.44, t +6.75). So the dashboard is built
around vol-selling setups, with the raw flow demoted to a secondary
idea-generation feed.

## 2. Honesty constraint (the governing principle)

The flow signals have **no proven mechanical edge** (Phase 3.6 backtests;
Study D `docs/study_D_result.md` showed the gamma+IV *conditioning* is weak OOS,
Welch +1.47). Therefore:

- The board is **descriptive/triage, never prescriptive.** No "buy/sell signal".
  Language is "vol is rich here, this is the classic-favorable structure — you
  build it", consistent with `webapp/gamma.py`'s existing "context, NEVER a
  signal" framing.
- The board ranks by vol **richness** (how much premium there is to sell), not
  by any claimed return.
- It must NOT lean on the long-gamma+high-IV "sell cell" (`vol_signal=="sell"`)
  as the ranker — that is exactly the conditioning Study D found weak. Regime is
  shown as **context only**.
- Every row carries a one-line caveat: the base vol premium is the tested edge;
  the regime is context, not extra return; size for a vol spike (fat left tail).

## 3. Non-goals (YAGNI for v1)

- No exact strikes / live-chain structure pricing (user chose the structure
  *template* level, not full strikes).
- No universe expansion beyond names already covered by live `gamma_regime`
  (expanding costs UW quota — out of scope for v1).
- No auto-trading, no alerts, no new routes.

## 4. Layout

Single existing dashboard page, two stacked sections:

```
┌────────────────────────────────────────────────────────┐
│  VOL-PREMIUM BOARD   (hero — where the edge is)          │
│  sorted by IV-rank ↓ ; earnings names demoted + flagged  │
│  ┌──────────────────────────────────────────────────┐   │
│  │ NVDA  IV-rank 88 ▮▮▮▮▮▮▮▮·  ATM IV 54%  ±15% /30d │   │
│  │ long-gamma (suppress) · call wall 1200 put 1050   │   │
│  │ ~30 DTE ~30Δ iron fly / put credit spread          │   │
│  │ "Vol top-decile of its year; rich to sell. No      │   │
│  │  earnings in window." · base premium is the edge,  │   │
│  │  regime is context · [Log to journal]              │   │
│  └──────────────────────────────────────────────────┘   │
│  … more names, descending IV-rank …                      │
│  ─────────── rich-vol threshold (IV-rank 75) ───────────  │
│  … dimmer rows below threshold …                         │
├────────────────────────────────────────────────────────┤
│  NOTABLE FLOW   (secondary — idea generation)            │
│  existing signal cards, sorted by notability             │
│  [Log this trade] on each                                │
└────────────────────────────────────────────────────────┘
```

## 5. Vol-Premium Board (hero)

**Universe:** the names that have a live `gamma_regime` row (the current
`LIVE_TICKERS` set fed by `gamma_live.gamma_refresh_loop`). No extra fetches.

**Sort:** `iv_pct` (IV-rank, 0..1) descending. A visual threshold line at
`iv_pct = 0.75` (the historically-rich cell); rows below are dimmed but shown.
**Earnings/catalyst demotion:** any name with an earnings date inside the ~30-day
structure window is flagged `⚠ earnings in window` and sorted *below* the clean
names (high IV before earnings is justified, not free premium — the classic
vol-selling trap).

**Per-row fields (all from `gamma_regime`, no new compute except expected move):**
- Name · **IV-rank** (`iv_pct`×100, the headline, with a bar) · ATM IV (`atm_iv`)
- **Expected move** ±% over ~30d = `atm_iv × √(30/365)` (≈ `atm_iv × 0.287`)
- Gamma regime tag (`regime`: long=suppress / short=amplify) — context only
- Call/put walls (`call_wall`/`put_wall`)
- **Structure template** (static text keyed off regime/richness): e.g.
  "~30 DTE, ~30Δ iron fly / put credit spread — collect vol premium, defined risk"
- **Plain-English read** (templated from the row's numbers)
- **Caveat line** (static, honesty)
- **[Log to journal]** CTA — prefills the journal with the name + structure
  template as the thesis (reuses the existing journal prefill mechanism).

## 6. Notable Flow (secondary)

The existing signal cards, under a "Notable flow" header, **sorted by a
descriptive notability composite** — `premium$ × aggressiveness(sweep + at-ask)
× clustering(repeat name/strike) × freshness × short-DTE`. No "buy" language;
each keeps its existing `Log this trade`. This is explicitly the idea-generation
/ discretionary-input layer, subordinate to the vol board.

## 7. Data & quota

- **Vol board core:** reads existing `gamma_regime` (iv_pct, atm_iv, net_gex,
  walls). **Zero new UW calls.**
- **Earnings flag (the one new dependency):** earnings dates are available via
  the existing `catalyst_calendar` provider (`GET /api/earnings/{ticker}` →
  `report_date`). They are not yet stored on `gamma_regime`. Add a nullable
  `next_earnings` column + one `/api/earnings/{t}` call per name inside
  `gamma_refresh_loop` (already market-hours-gated by Phase 4.28, ~10 names /
  4 min → negligible quota).
- **Notability:** computed from fields already on each signal row; no new fetch.

## 8. Components & boundaries

- `webapp/vol_board.py` (new) — **pure** assembly: given `gamma_regime` rows +
  earnings dates → ordered list of `VolBoardRow` (expected-move, regime tag,
  structure-template key, read text, earnings flag, below-threshold flag).
  Fully unit-testable with no DB/HTTP.
- `webapp/explanations.py` (extend) — vol-board read-text + structure-template
  + caveat strings (keeps copy in one place, like the existing AXES/GLOSSARY).
- `webapp/gamma_live.py` (extend) — fetch + persist `next_earnings` per name.
- `webapp/gamma.py` (extend) — `next_earnings` column on the model + view.
- `webapp/notability.py` (new) — pure notability score over a signal row;
  unit-testable.
- `webapp/main.py` (extend) — dashboard route passes vol-board rows + notability
  ordering to the template.
- `webapp/templates/dashboard.html` (extend) — vol-board hero partial on top;
  existing cards moved under "Notable flow".

## 9. Testing

- `vol_board`: ordering (IV-rank desc), earnings demotion, threshold flag,
  expected-move math, empty/None handling. Pure, deterministic.
- `notability`: monotonic in each factor; stable ordering.
- `gamma_live` earnings fetch: degrades to None on UW error (no crash, like the
  existing axes — D7).
- A webapp route smoke test that the dashboard renders with vol-board present
  (extends the existing route smoke tests).
- No threshold tuning; no change to existing scoring (D4/D8).

## 10. Open items for planning

1. Exact earnings-window definition (~30d to match the structure template?) and
   whether to also demote on non-earnings catalysts (fed/M&A) — lean: earnings
   only for v1.
2. Whether `next_earnings` lives on `gamma_regime` (simplest) vs a small
   separate table — lean: column on `gamma_regime`.
3. Structure-template copy per regime (long-gamma vs short-gamma) — finalize the
   exact wording with Berkay during implementation.
