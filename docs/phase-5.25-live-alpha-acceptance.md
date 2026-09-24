# Phase 5.25 — Live Alpha v1 acceptance

Status: frozen when the first 5.25 code commit lands (2026-09-24).
Owner approval: the master prompt `UOA_Canli_Alfa_Urununu_Bitir_Master_Prompt.md`,
given in chat on 2026-09-24 with the order to build, merge and deploy. The
product decision is recorded in `docs/live-alpha-v1/PRODUCT_OVERRIDE.md`.

## 1. Scope

A live decision screen at `/`, "Bugünün Fırsatları". It turns data the app
already stores, plus two new Unusual Whales reads (news headlines, contract
quotes for a shortlist), into reasoned recommendations. Each recommendation
has a numeric stock plan and a priced option alternative. Every
recommendation is stored as an immutable record and followed by a PAPER
tracker. The old Alfa Board moves to `/alfa`. Every other route is unchanged.

Out of scope: broker connection, automated orders, naked short, ratio or
calendar structures, 0DTE, LLM text, parameter optimisation, new paid data.

## 2. Modes (four independent axes)

```
market_mode:         LIVE / PREMARKET / CLOSED / DEGRADED
recommendation:      BUY / CONDITIONAL_BUY / WATCH / AVOID / BEARISH_SETUP / EXIT_REVIEW
instrument_readiness: READY / TRIGGER_PENDING / QUOTE_PENDING / RISK_BLOCKED / INVALID
evidence_status:     EXPERIMENTAL_RULES (the only value v1 can produce)
tracking:            OBSERVED / PAPER_PENDING / PAPER_OPEN / EXIT_SIGNALLED /
                     CLOSED_SIMULATED / MANUAL_FILL_RECORDED / UNRESOLVED
```

Publication modes: `LIVE_ANALYSIS` (the recommendation), `PAPER_TRACKING`
(the simulated follow-up), `MANUAL_FILL` (the owner's own trade, recorded
separately) and `BROKER_EXECUTION` (closed).

## 3. Inputs, with source and time

| Input | Source | Time fields kept |
|---|---|---|
| Option flow | `signal` + `alfa_print_meta` of the newest `live-*` run (already written by the live worker from `/api/stock/{t}/flow-alerts`) | print `timestamp` (provider event time), run id |
| Spot | `alfa_atm.stock_price` (refresher, `/atm-chains`) | `fetched_at` |
| Daily bars, ATR, previous close | `alfa_daily_bar` (daily_close job) | bar `day`, `fetched_at` |
| Expiries | `alfa_atm_expiry` | `fetched_at` |
| News | NEW: `GET /api/news/headlines?ticker=` into `alfa_live_news` | provider `created_at` (kept as `provider_created_at`, never renamed to a first-publication time), our `first_seen_at` |
| Contract quotes | NEW call to the existing `/api/stock/{t}/option-contracts?option_symbol[]=` for constructed candidate symbols | our `fetched_at`. The endpoint carries no quote timestamp, so the screen says "UW NBBO, alındı HH:MM" |

News has no URL field in the UW schema. The screen shows the provider's
source name, headline and time, and never builds a link.

The rules:
- A failed read is a state, never neutral or supportive evidence. The states
  are `not_checked`, `failed`, `stale`, `checked_none` and `checked_found`.
- Outside the regular session, the NBBO of `option-contracts` belongs to the
  last session. Option readiness is then `QUOTE_PENDING`, never `READY`.

## 4. Market session

`src/uoa_detector/live_alpha/calendar.py` handles the session clock:
- It uses the NYSE full-day holidays and 13:00 ET early closes for 2026 and
  2027, listed in the profile.
- Times are converted to ET with `zoneinfo`, so DST is exact.
- It classifies the session as PREMARKET (04:00–09:30 ET), LIVE, or CLOSED
  (after the close, weekends and holidays).
- A year missing from the profile makes the mode `DEGRADED` ("takvim
  bilinmiyor"). It never guesses.

## 5. Decision policy `live_alpha_v1` (profile `profiles/live_alpha_v1.yaml`)

Every threshold is a design choice made on economic grounds, not a fitted
optimum.

**Flow summary per ticker.** It is computed from the selected run's prints.
- Prints with the same `option_chain` in the same minute are one print
  (`dedupe_seconds`). Repeated prints of one sweep are not independent
  confirmation.
- Direction is side-aware (`webapp.board.direction.direction_for`).
- The summary gives: directional premium up and down, the side-aware share,
  distinct contracts, first and last print time, and the largest print.

**Flow direction qualifies** when all of these hold:
- premium in the direction is at least `min_directional_premium_usd`;
- its share of side-aware premium is at least `dominance_min`;
- the side-aware share of all premium is at least `side_aware_share_min`;
- there are at least `min_distinct_contracts` distinct contracts.

**Price context.**
- `move = spot / prev_close - 1`
- `move_atr = (spot - prev_close) / ATR14`
- `relative = move - SPY move`
- Spot is fresh when its age is at most `spot_max_age_seconds`, and only in a
  LIVE session.

**Paths.** The first path that matches decides. The order is fixed.

1. **P1 news_continuation.** The flow qualifies up, and a news check
   `checked_found` has a ticker headline within `news_window_hours`. The price
   confirms (`move_atr >= confirm_min_atr`), and the price is not extended
   (`move_atr <= chase_max_atr`). Result: BUY.
2. **P2 market_relative_flow.** The flow qualifies up and
   `relative >= relative_min`. No sector data exists, so the screen labels the
   comparison "piyasaya (SPY) göre". The price confirms and is not extended.
   Result: BUY.
3. **P3 pullback_entry.** The P1 or P2 thesis holds but
   `move_atr > chase_max_atr`. Result: CONDITIONAL_BUY with an entry zone at
   or below `prev_close + chase_max_atr × ATR`.
4. **P4 bearish_setup.** The flow qualifies down, and the price confirms down
   (`move_atr <= -confirm_min_atr`). Result: BEARISH_SETUP. The stock view is
   "alımdan kaçın". The alternatives are a long put or a bear put spread. No
   short sale is assumed.
5. The flow qualifies in either direction, but P1–P4 fail. Result: WATCH,
   with the named blocker, for example "fiyat akışla ters" or "haber kontrol
   edilemedi ve SPY'a göre ayrışma yok".
6. The price contradicts a qualifying up flow (`move_atr <= -confirm_min_atr`).
   Result: AVOID.

**Readiness and closed sessions.**
- BUY is READY only in a LIVE session with fresh spot.
- In PREMARKET and CLOSED sessions, BUY and CONDITIONAL_BUY become
  CONDITIONAL_BUY with `TRIGGER_PENDING`, and the card shows the next
  session's trigger. A stale spot in LIVE gives `TRIGGER_PENDING` with the
  blocker "fiyat bayat".

**Derived from.** Every record carries `derived_from = ["phase-3.6
directional UOA confluence (REJECTED)", "options_alpha_v1 H01/H02 flow
families (REJECTED)"]`.
- The real design difference: the entry requires fresh price confirmation, a
  chase limit, and either news or market-relative strength.
- The execution path is the stock by default, and the option only when its
  cost passes §7.

## 6. Stock plan

- **Entry.** Fresh spot. Chase limit: `prev_close + chase_max_atr × ATR`.
- **Stop.** `entry − stop_atr × ATR`.
- **Target.** `entry + target_r × (entry − stop)`, labelled "politika hedefi
  (2R)" and never a forecast.
- **Horizon.** `horizon_sessions`.
- **Size.** From the labelled PAPER profile `r_usd`, with the notional capped
  at `max_notional_usd`. It is not the owner's account.
- **Bearish setup.** There is no stock entry. The stop and target levels are
  mirrored for the put alternative's underlying trigger.

## 7. Option alternative

**Selection.**
- **Expiry.** The first listed expiry in `alfa_atm_expiry` with DTE in
  `[min_dte, max_dte]`.
- **Strikes.** Candidate strikes are built on a grid inferred from the listed
  ATM strike and the run's printed strikes for the ticker. They are verified
  by UW returning the symbol. A dropped symbol does not exist.
- **Long leg.** The listed strike nearest spot, on the side of the thesis.
- **Short leg.** The listed strike nearest the stock plan's target.

**Structures.** Long call, bull call debit, long put, bear put debit.

**Pricing.** `options_alpha.structures.price_structure`, using the frozen
cost model of `options_alpha_v1.yaml`:
- a long leg opens at the ask and closes at the bid; a short leg opens at the
  bid and closes at the ask;
- 2 % latency on each side;
- commission of $0.65 per contract, per leg, per direction.
The spread is never charged twice.

**Readiness.**
- `QUOTE_PENDING` when a leg has no quote, the quote is not from a LIVE
  session, or the quote is older than `quote_max_age_seconds`.
- `RISK_BLOCKED` when one lot's max loss is above `r_usd`. The card then shows
  that one lot's risk.
- `INVALID` when no listed contract fits.

**Preference rule.**
- **Stock** when every option structure is unready, or its round-trip cost is
  above `max_roundtrip_cost_pct` of the debit. The round-trip cost is
  `(entry_debit − exit_credit) / entry_debit` plus commission.
- **Debit spread** when the long option fails the budget or the cost rule and
  the spread passes.
- **Long option** when it passes both.
- The reason is written out in each case.
- One opportunity has one PAPER position, in the preferred instrument.

## 8. Records and tracking

New tables (created with `checkfirst`, append-only unless a row says
otherwise):
- **`alfa_live_scan`.** One row per cycle: mode, funnel counts, timing,
  request use, status and the snapshot JSON the page renders. The page reads
  the newest row only.
- **`alfa_live_rec`.** Immutable. A new row appears only when an
  opportunity's (recommendation, readiness) pair changes. It holds the full
  card JSON, `policy_version`, `inputs_hash` and `supersedes`.
- **`alfa_live_news`.** Headlines, unique on the hash of (source, headline,
  provider_created_at).
- **`alfa_live_paper`.** One row per opportunity. Its state and exit fields
  are updated only through `alfa_live_event` (append-only).
- **`alfa_live_event`.** Every state change: who wrote it (job or owner),
  when, the policy version and the reason.
- **`alfa_live_manual`.** The owner's own fills, kept apart from PAPER.
- **`alfa_live_heartbeat`.** One row per cycle.

**PAPER fill semantics.**
- A BUY/READY recommendation opens `PAPER_PENDING`.
- The next cycle fills it only in a LIVE session, with fresh data, and at or
  below the chase limit. A stock fills at `spot × (1 + slippage_bps)`. An
  option fills at the pricing of §7 on fresh quotes.
- The exits are stop, target, time (`horizon_sessions`), or thesis broken (a
  down flow qualifying on the same ticker).
- An exit fills at `spot × (1 − slippage_bps)`, or at the option's exit
  credit.
- A missing exit price gives UNRESOLVED, never a zero or a free exit. If stop
  and target are both crossed between two samples, the stop wins.

## 9. Quota

Every new UW call first calls `webapp.board.quota_ledger.reserve`, which is
atomic and DB-backed, against the board's `daily_request_soft_cap`.
- A refusal skips the call and records `quota_refused`.
- A reservation is not refunded after a timeout.
- News per ticker is refreshed at most once per `news_refresh_seconds`.
- Contract quotes are fetched only for tickers whose recommendation needs an
  option plan.

## 10. Screen

The page reads the newest `alfa_live_scan` in one query. It renders in this
order:
1. mode and data time in ET and TR;
2. the one-line view of the day;
3. the cards;
4. conditional and watch cards;
5. open PAPER positions;
6. the funnel (taranan → akış uygun → fiyat verisi uygun → plan → giriş
   hazır);
7. sources and technical status.

A card follows the §11 schema of the master prompt. Actions: izle, pas geç,
manuel fill kaydı. Each is a POST behind Basic auth and the same-origin check.

## 11. Tests (minimum)

- Pure arithmetic, checkable by hand: the stock plan; spread pricing; the
  preference rule.
- Positive: a synthetic fresh LIVE input reaches BUY/READY through P1 and
  through P2.
- Negative:
  - stale spot is never READY;
  - a closed market is never READY;
  - a failed news read is never "haber yok";
  - a dropped leg gives QUOTE_PENDING;
  - a crossed quote is refused;
  - a non-100 multiplier is carried through;
  - one opportunity never gets two PAPER positions;
  - a changed decision appends a record and never overwrites;
  - the quota refusal path;
  - an unauthenticated request gets 401;
  - scan snapshots survive a restart;
  - the legacy honesty guard still holds on `/alfa`.

## 12. Done when

Every row of §21 of the master prompt is backed by evidence or marked
BLOCKED with the real external cause, in `docs/live-alpha-v1/STATUS.md`.
