# Phase 5.3 — Spot decision frame

**Status: frozen on approval. Contract, not a plan (D1).**
**Owner decisions pinned 2026-09-17. Author: Claude Code.**

---

## 1. Objective

The board answers the question its owner actually asks.

Today every action cell prices an **option**: the tradability chip reports the
option's bid/ask spread, the İŞLENMEZ gate hides rows whose *option* is too wide,
the size cell counts *lots* of 100 shares' worth of premium, and B2 states the
move required for the *option* to break even.

The owner trades the **underlying**. He buys and sells shares. Every one of those
cells answers a question he never asks, which is why the page reads as
"anlamıyorum ne dediğini, neden alacağımı".

The options flow stays exactly what it is: **the evidence**. What changes is the
**action**. After this phase a row says:

> NVDA · yukarı · giriş 184.20 · stop 179.85 (−%2,4) · 22 adet · riskin $95,70
> · beklenen hareket ±%3,1 → 1,3R · ATR(14) 2,90

and, folded under `Opsiyon detayı`, everything the board says today.

This phase adds no new evidence, no new score and no new UW endpoint. It
re-frames what is already computed into the units the owner trades in.

---

## 2. Owner decisions (pinned; these are the test condition, not knobs)

- **O1 — Stop is ATR-based.** `stop = entry − atr_stop_multiple × ATR(period)`
  for a long, `entry + …` for a short. Chosen over structural levels (needs a
  "last swing" parameter that behaves differently per name), fixed percent (too
  tight on volatile names, too loose on calm ones) and strike-invalidation
  (elegant but leaves stop distance uncontrolled, %1 on one row and %12 on the
  next).
- **O2 — Spot is the default view.** The option frame folds under
  `Opsiyon detayı`. **The İŞLENMEZ cost gate never hides a row in the spot
  view**: an option too wide to trade says nothing about the tradability of the
  stock, and hiding the row on that basis is the single most misleading thing
  the current page does to a spot trader.
- **O3 — Capital $10,000, risk per trade 1% ($100).**
  `sizing.values_confirmed_by_owner` becomes `true`, so `(varsayılan değer)`
  disappears and every count becomes a real number.

These three were answered by the owner on 2026-09-17. Changing any of them is a
new phase, not an edit here (D1).

---

## 3. Architecture

### 3.1 The bar table (new)

`alfa_daily_bar`, primary key `(ticker, day)`, **append-only** and classified as
such in `test_alfa_card_durability.py` (P38):

| column | |
|---|---|
| `ticker`, `day` | PK |
| `open`, `high`, `low`, `close` | `float \| None` — a row with no parseable high or low is stored with nulls, never skipped and never guessed |
| `fetched_at` | when |

**It costs no additional UW request.** The `daily_close` job
(`webapp/board/daily_close.py:211`) already fetches
`GET /api/stock/{ticker}/ohlc/1d` for every board ticker plus SPY, every day.
`webapp/ohlc.py:regular_session_closes` currently parses that payload and keeps
`close` alone. This phase adds `regular_session_bars`, which keeps
open/high/low/close from the identical `market_time == "r"` rows, with the same
explicit ordering and the same drop rules.

`alfa_daily_close` is **not** modified. It is append-only and load-bearing for
the outcome job; a second reader does not justify touching it (D10, P38).

### 3.2 `webapp/board/spot.py` (new, pure)

No I/O, no UW call, no profile read of its own — the caller passes settings, as
`moves.py` and `sizing.py` already do.

- **True Range** per session: `max(high − low, |high − prev_close|, |low − prev_close|)`.
  A session missing any of the three inputs yields no TR and breaks the window.
- **ATR** = Wilder's smoothing over `spot.atr_period` sessions.
- **Entry** = the current spot (`alfa_atm.stock_price`, the same price B2 already
  uses, so the two cells can never disagree).
- **Stop** = `entry ∓ spot.atr_stop_multiple × ATR`.
- **Shares** = `floor(risk_usd / (entry − stop))`, `risk_usd = capital × risk_pct`.
- **Risked** = `shares × (entry − stop)` — the real number, which is at or below
  `risk_usd` because of the floor.
- **Target** = the expected move `moves.py` already computes from the ATM
  straddle. Nothing new is derived.
- **R to target** = `(target − entry) / (entry − stop)`.

### 3.3 Profile keys (D8)

New `spot:` block in `profiles/board_v1.yaml`, strict model in
`webapp/board/settings.py`:

```yaml
spot:
  atr_period: 14                  # Wilder ATR window, in regular sessions
  atr_stop_multiple: 1.5          # stop distance = this x ATR (owner decision O1)
  atr_min_sessions: 20            # fewer stored sessions than this: no ATR, no stop, no count
  max_position_pct_of_capital: 25 # one position may not exceed this share of the account
```

And `sizing.capital_usd: 10000`, `sizing.r_usd: 100`,
`sizing.values_confirmed_by_owner: true` (owner decision O3).

---

## 4. Binding honesty rules

These extend §2 of the Alfa Board contract and carry the same weight.

- **R-SP1 — No bars, no stop.** Fewer than `spot.atr_min_sessions` usable
  sessions → ATR, stop, share count and R are all `bilinmiyor`, and the row says
  why (`yeterli günlük bar yok`). A guessed stop is worse than no stop: it is a
  number the owner would size against.
- **R-SP2 — No stop, no size.** A zero or negative stop distance yields no share
  count. Never a fallback percentage.
- **R-SP3 — Zero shares is rendered.** When one share costs more than the risk
  allows, the count reads `0` and the row stays on the board. That is a fact
  about the account, not a reason to hide a candidate.
- **R-SP4 — The target is an expected move, not a forecast.** It inherits R-EV1:
  no probability is stated anywhere, in any wording.
- **R-SP5 — The frame never says "al" or "sat".** It states entry, stop, size,
  target and what would falsify the idea. The decision stays the owner's; this
  is the same rule the whole board runs under and the spot units must not soften
  it.
- **R-SP6 — The position cap is disclosed, never silent.** If
  `shares × entry > max_position_pct_of_capital % × capital`, the count is capped
  and the row states that it was capped and by what.
- **R-SP7 — The option gate does not hide a spot row.** İŞLENMEZ stays visible
  inside `Opsiyon detayı` and never removes a row from the spot view (O2).
- **R-SP8 — Stale spot, no frame.** If the spot price behind `entry` is older
  than `tradability.max_quote_age_seconds`, the whole spot cell reads
  `bilinmiyor` with the age shown. An entry priced off a stale quote sizes a real
  position off a number that no longer exists.

---

## 5. The decision card

`POST /alfa/card` freezes the row's rebuilt view. The spot frame must be inside
that frozen object — entry, stop, ATR, share count, risked dollars, target and R
— so a card opened in three months explains the decision in the terms it was
made in.

`alfa_decision_card.card` is a JSON blob, so this is additive: no column, no
migration, no schema change. The existing parity test (the card's `row_view.row`
equals the live row) extends to the new fields.

---

## 6. Tests

1. **ATR arithmetic** against a hand-computed Wilder series; a gap in the bars
   breaks the window rather than silently spanning it.
2. **R-SP1** — one session short of `atr_min_sessions` yields `bilinmiyor` for
   every derived cell, and the reason string.
3. **R-SP2 / R-SP3** — zero stop distance yields no count; an unaffordable share
   yields a rendered `0`.
4. **R-SP6** — the cap binds and is disclosed.
5. **R-SP7** — an İŞLENMEZ row is present in the spot view and absent from
   neither.
6. **R-SP8** — a stale spot kills the frame.
7. **Bar parsing** — the same `ohlc/1d` fixture feeds `regular_session_closes`
   and `regular_session_bars`, and their closes are identical. The two readers
   can never drift.
8. **Durability** — `alfa_daily_bar` is classified append-only and the scan
   still passes (P38).
9. **Card parity** — the frozen card carries the same spot numbers the page
   showed.
10. **Budget** — a test asserts the daily job's UW request count is unchanged by
    this phase.

Every test states the rule it pins in its docstring, and any test claiming to
pin a guard must fail with the guard removed (P39/P40).

---

## 7. Commit plan

| N | |
|---|---|
| 5.3.1 | `regular_session_bars` + `alfa_daily_bar` + the daily job writing it; no UI |
| 5.3.2 | `webapp/board/spot.py` + profile block + strict settings |
| 5.3.3 | Row view: spot cells, the `Opsiyon detayı` fold, R-SP1–R-SP8 |
| 5.3.4 | Card freezing + parity test |
| 5.3.5 | `values_confirmed_by_owner: true` and the copy that drops `(varsayılan değer)` |
| 5.3.6 | Closeout `docs/phase-5.3-closeout.md` |

Each green on its own (D2). Each a PR; the owner merges.

---

## 8. Non-goals

- **No order routing, no broker connection, no execution of any kind.** This
  phase prints numbers on a page. Nothing is sent anywhere.
- **The options frame is not deleted.** It folds. The flow is still the evidence
  and the owner may want it back in view.
- **No new evidence family, no scoring change, no threshold change** (D4/D8).
- **No edit to the 5.2 contracts.** They stay frozen; this is a new phase
  precisely because they are (D1).
- **Registry note.** 5.3 was allocated as "Ticker detail", unstarted. A per-ticker
  spot frame is that scope's substance, so the row is redefined rather than
  renumbered. If the owner prefers a separate number, this doc moves with no code
  impact.
