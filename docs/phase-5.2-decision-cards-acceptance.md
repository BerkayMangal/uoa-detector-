# Phase 5.2 FAZ C — Decision cards, pass ledger, fill capture (acceptance contract)

Status: **FROZEN** (per D1) from its first commit. Owner: Berkay. Author: Claude (Opus 5).

Berkay's written spec of 2026-09-15 (FAZ C) is the approval. Its FAZ C
section, in his words: "bugün kaydetmezsen veri sonsuza kadar kayıp — atlama".
The spec also asked for this separate contract, because decision cards were
expected to extend `StoredSignal` / `BacktestStoreProtocol`.

## 1. Decision: no protocol change

Discovery showed that extending `StoredSignal` would break rollback.
- `StoredSignal` is `extra="forbid"` on `main` and on `phase-3-final`
  (`src/uoa_detector/backtest/store.py:34`).
- A null-valued new key already fails validation there, so a rollback would
  silently drop every new row from the page.

Decision cards therefore live in **new append-only tables**. `StoredSignal`,
`BacktestStoreProtocol`, `SignalRow` and `TradeRow` are unchanged.

To undo: drop the three `alfa_` tables after exporting them. Nothing else
references them.

## 2. Tables (append-only; never dropped, reset or rewritten)

| Table | Columns | Key |
|---|---|---|
| `alfa_decision_card` | id (uuid hex), created_at (UTC), decision (`log` \| `pas`), ticker, direction (`yukarı` \| `aşağı`), run_id, dominant_option_symbol, card_json (frozen row view), board_version (git sha from `RAILWAY_GIT_COMMIT_SHA` when set, else `unknown`), board_profile_hash, calibration_profile_hash, trade_id (nullable, set when a Log becomes a journal trade), note (short, optional) | id |
| `alfa_fill` | id, card_id, trade_id (nullable), created_at, side (`giriş` \| `çıkış`), fill_price, contracts, assumed_ask, assumed_bid, assumed_mid (from the card snapshot), quote_age_seconds_at_card | id |
| `alfa_outcome` | card_id, horizon_days, computed_at, underlying_close_at_card_day, underlying_close_at_horizon, spy_close_at_card_day, spy_close_at_horizon, market_neutral_excess (direction-signed), option_symbol, option_bid_at_horizon (nullable), status (`bekliyor` \| `hesaplandı` \| `veri yok`) | (card_id, horizon_days) |

An `alfa_outcome` row moves from `bekliyor` to a final status exactly once.
Final rows are never rewritten.

## 3. C1 — Decision card

- **Buttons.** Every board row has `Logla` and `Pas geç`.
  - Each POSTs the row's identity; the server rebuilds the row's view model
    from the database at that moment (it trusts no client values) and freezes
    it into `card_json`.
  - The frozen view holds:
    - the evidence strip and states, and the evidence word;
    - tradability state, spread %, round-trip dollars, lot % of capital, exit
      depth and quote ages;
    - the regime sentence and tripwires;
    - the chase verdict, required vs expected move, opening/closing state and
      catalyst chip;
    - the penalty ledger;
    - the reason and `AMA` sentences;
    - the constituent `(run_id, event_id)` list.
- **Log flow.** `Logla` writes the card, then redirects to `/journal/new`
  with the card id prefilled. When the trade is saved, the journal POST
  stores the new trade id on the card.
  - The journal route and `TradeRow` are unchanged, apart from an optional
    hidden `card_id` form field.
  - The `trade_id` link is set once, on a card that has none. Setting a
    missing link does not change the card snapshot.
- **Pas flow.** `Pas geç` writes the card with decision `pas`. That is the
  pass ledger (C2).
- **No UW calls.** Card writes make no Unusual Whales call.
- **POST hardening.** Card and fill POSTs require an `Origin` or `Referer`
  header matching the request host (a CSRF guard on top of Basic auth).

## 4. C2 — Pass (shadow) ledger

`/defter` lists cards newest first with filters for decision (log / pas) and
ticker. Each card shows its frozen snapshot and its outcomes.

**Counterfactual outcomes.** The daily post-close job computes outcomes for
each horizon in `outcomes.horizons_trading_days` (pinned [1, 5]), once the
horizon has passed.
- **Primary: market-neutral excess.** Direction sign × ((U_h / U_0 − 1) −
  (SPY_h / SPY_0 − 1)), from `alfa_daily_close`. This is the method the journal
  already uses (`webapp/journal.py` `directional_excess`).
- **Secondary: the dominant contract's bid on the horizon day**, from
  `/api/option-contract/{occ}/historic`. It is `veri yok` when the contract did
  not trade that day. The probe found NBBO is null on zero-volume days, so no
  option P&L is invented.

Taken (`log`) and passed (`pas`) cards use identical math, so they stay
comparable.

**Aggregates.** Nothing is aggregated unless each group has at least
`fills.min_n_for_stats` cards with final outcomes. Below that, only counts are
shown (`12 pas, 3 log; istatistik için yetersiz örnek`). No hit rate,
average or t-statistic appears below that sample size.

## 5. C3 — Fill capture

- **Form.** On a logged card or journal trade, `Dolum gir` records the actual
  fill price, contracts and side.
- **Slippage.** For an entry it is fill − assumed_ask; for an exit,
  assumed_bid − fill. Both are in dollars per contract and as % of mid, using
  the card snapshot. This is the live test of the cost assumption.
- **Assumed quote.** No source gives the NBBO at the moment of a manual fill:
  option-contracts has no quote time, and `/flow` is frozen at the last print.
  So the assumed quote is the card's snapshot quote, labelled with its
  `fetched_at` age (`kart anındaki kotasyon, {n} sn yaşında`), never
  "NBBO at fill".
- **Display.** Counts only below `fills.min_n_for_stats` fills
  (`4 dolum kaydı; istatistik için yetersiz örnek`). At or above it, the
  median and interquartile range of slippage are shown next to the assumed
  spread. No significance claim is made.

## 6. Tests

- Card POST snapshot equals the rebuilt row view; client-sent values are
  ignored.
- Append-only: no update or delete path exists in the repo API, and outcome
  status advances only from `bekliyor` to a final status.
- Origin/Referer guard: 403 on mismatch.
- Outcome math on hand-computed fixtures.
- The count-only display below the sample threshold, and aggregates
  appearing only at or above it.
- The auth enumeration test automatically covers the new routes.
- A live integration test on the daily-close and historic endpoints used for
  outcomes.

## 7. Commit plan

| # | Scope |
|---|---|
| 5.2.C1 | tables, card POSTs (`Logla` / `Pas geç`), journal `card_id` link |
| 5.2.C2 | `/defter` page, outcome job, count-gated aggregates |
| 5.2.C3 | fill capture form and slippage display |
