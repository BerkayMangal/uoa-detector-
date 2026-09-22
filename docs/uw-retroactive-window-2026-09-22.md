# UW retroactive window, re-measured 2026-09-22

**Type: STATUS.** A measurement, not a contract. It does not change any verdict
and it does not authorise any work; it records what the Unusual Whales
subscription actually answers today, because a load-bearing clause in
`docs/INDEX.md` §0 rests on a single reading taken four months ago.

Reproduce with `uv run python scripts/probe_uw_retroactive_window.py [TICKER]`.

---

## 1. Why this was measured

`docs/phase-3.5.5-status.md` B1 recorded, from a probe on **2026-05-18**:

> `historic_data_access_missing` — "The earliest date currently available to you
> is 2026-05-07 (7 trading days)."

That reading became the canonical clause in `docs/INDEX.md` §0 — *"UW-fed Track B
(v5_gamma_squeeze with real UW enrichment) has never been testable, because UW
history is about 7 days"* — and it is load-bearing. It is the stated reason the
project's own central thesis was never run, and it is why a forward capture job
looked like the only route to a Track B dataset.

A single reading cannot distinguish two very different worlds:

- a **rolling** ~7-trading-day window, which never accumulates; or
- a **fixed floor** that happened to sit ~7 trading days back on the day it was read,
  and has been accumulating behind it ever since.

On 2026-05-18 those two are indistinguishable. Four months later they are not,
and the difference is one cheap measurement.

## 2. What was measured

Direct HTTPS GETs against `api.unusualwhales.com`, read-only, roughly 30 requests
total. Two questions, because the first is worthless without the second:

1. Does a **dated** request succeed for a past session?
2. Does the payload carry **the date that was asked for**?

Question 2 is the one that matters. `_decode_spot_rows`
(`src/uoa_detector/sources/unusual_whales/providers/dealer_gamma.py`) applies no
date check, unlike its sibling `_decode_strike_rows`. A vendor that silently
ignored `date=` would return today's rows, and any capture built on it would file
today's data under a past session date — invisible a year later, because
`row_count`, `pages` and `truncated` would all look healthy.

## 3. Results

### 3.1 `date=` is honoured, and the payload carries the requested day

`spot-exposures`, SPY:

| Asked for | Result |
|---|---|
| 2026-09-21 (~1 session back) | 200, 470 rows, all dated 2026-09-21 |
| 2026-09-09 (~8) | 200, 562 rows, all dated 2026-09-09 |
| 2026-08-31 (~15) | 200, 526 rows, correct date |
| 2026-08-10 (~30) | 200, 521 rows, correct date |
| 2026-06-26 (~60) | 200, 526 rows, correct date |
| 2026-04-02 (~120) | **HTTP 403** |
| 2025-09-22 (~250) | **HTTP 403** |
| *(no `date=` param)* | 200, 470 rows, dated 2026-09-21 — the last completed session |

Not one 200 came back carrying a day other than the one requested. The control
confirms the parameter genuinely filters rather than being ignored.

### 3.2 The floor sits at 2026-05-12, and it is account-wide

| Asked for | Result |
|---|---|
| 2026-05-08 | HTTP 403 |
| 2026-05-11 | HTTP 403 |
| **2026-05-12** | **200, 470 rows, correct date** |
| 2026-05-13 | 200, 512 rows, correct date |
| 2026-05-14 | 200, 482 rows, correct date |

Not SPY-specific — AAPL and NVDA behave identically (2026-05-15 answers with 377
rows each; 2026-05-07 is 403 for both).

### 3.3 Every axis Track B needs is equally deep

At 2026-06-26 (~60 sessions back), SPY:

| Endpoint | Result |
|---|---|
| `spot-exposures` | 200, 526 rows, correct date |
| `greek-exposure/strike` | 200, 547 rows, correct date |
| `ohlc/1m` | 200, 827 rows, correct date |
| `darkpool` | 200, 50 rows (page limit), correct date |
| `option-trades/flow-alerts` (windowed `newer_than`/`older_than`) | 200, 50 rows (page limit), correct date |

`ohlc/1m` legitimately reports two calendar days, because the extended session
runs to 23:59 UTC and crosses midnight. That is the session's shape, not
contamination.

## 4. The window is not rolling

On 2026-05-18 the floor was **2026-05-07**. On 2026-09-22 it is **2026-05-12**.

The floor advanced about **3 trading sessions** while the calendar advanced about
**90**. A rolling 7-trading-day window would put today's floor near 2026-09-11; it
does not. Whatever the retention mechanism is, its behaviour over four months of
elapsed time is a near-fixed anchor, not a sliding window.

**Consequence:** roughly **95 trading sessions** of UW history — 2026-05-12
through 2026-09-21 — are reachable right now, on exactly the axes the Track B
thesis scores: dealer gamma (per-minute and per-strike), 1-minute underlying
bars, dark pool prints, and the flow alerts themselves.

## 5. What this does NOT establish

Stated explicitly, because the temptation here is to over-read a good result.

- **The original finding is not shown to have been wrong when it was taken.** On
  2026-05-18, 2026-05-07 really was about 7 trading days back. What is now shown
  is that the figure did not mean what it was taken to mean.
- **The exact mechanism is unknown.** Fixed anchor, a retention period that
  happens to start there, or a subscription change between May and September —
  this measurement cannot separate them, and the ~3-session drift is unexplained.
  If it is a slow retention, the oldest sessions will eventually age out.
- **Not every endpoint was probed.** IV-rank is documented as having no history
  before 2026-05-04 (`docs/phase-3.9-closeout.md` §7 item 7) and was not
  re-tested. The endpoint that produced the original
  `historic_data_access_missing` message was not re-probed in the form that
  produced it.
- **Depth is not completeness.** A 200 with the right date says the session is
  reachable. It does not say the payload is complete: `darkpool` and
  `flow-alerts` both returned exactly their page limit here, so paging and
  truncation behaviour at depth is unmeasured.
- **Reachable is not free.** A bulk harvest of 95 sessions across a real universe
  is thousands of requests against a 30,000/day limit whose true daily headroom
  is itself unmeasured — the repo holds only two partial-day readings, both taken
  before the board refresher was live.
- **This says nothing about edge.** It changes what is *testable*, not what is
  *true*. The canonical statement stands: no tradeable edge found.

## 6. What it changes

The premise of `docs/INDEX.md` §0's last clause is stale. Whether UW-fed Track B
is closed or open was already an open item for Berkay (§7); it is now an open item
with a different fact underneath it, because the reason recorded for never
running it — that the history does not exist — is not what today's subscription
answers.

It also reframes any daily-capture work. Capturing forward was argued for on the
grounds that the past cannot be bought. About 4.5 months of that past is sitting
there today, and harvesting it is a bounded one-off rather than a twelve-month
wait. What capture still buys that a harvest cannot is everything below the
floor, plus point-in-time fields that are overwritten rather than dated
(sector membership, earnings estimates, quotes and ATM rows the board rewrites
every 300 seconds, and `gamma_regime`, which is dropped on every process start).

No phase is allocated and no work is authorised by this document.
