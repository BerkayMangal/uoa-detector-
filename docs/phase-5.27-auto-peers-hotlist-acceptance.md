# Phase 5.27 — automatic peers, hot list and lag record (`auto_peers_v1`)

**Type: FROZEN.** Registry slot 5.27 (`docs/INDEX.md` §7). Written before any
code of this phase. Frozen from the owner's approval on 2026-10-12 (D1): a
decision found wrong during implementation is flagged and recorded in a new
document, never edited into this one.

**Owner approval, 2026-10-12.** Berkay read the 5.26 column in production, found
that it covered only the five hand-written optical names and said what he
actually wants: the lag measure for **every** stock on the board, and the
system choosing hot stocks **by itself** (his words, 11:48Z: "her hisse icin
olmasi lazim, hisseleri bazen onun otomatik secmesi lazim"). The plan below was
put to him and he answered "evet abi al dedigim iste canliya" (12:05Z): build
it and put it live without a second round. This document is that plan, frozen.

It extends 5.26 (`docs/phase-5.26-basket-relative-acceptance.md`, frozen and
untouched). 5.26 §1 and §4 named hot-spot discovery as the next phase; this is
it. The 5.26 column, its profile and its numbers do not change.

---

## 1. What this phase builds, and what it does not

**Builds.**

1. **Automatic peers** for every row of the "Hisse panosu": the names that
   moved most like the ticker over the last 60 sessions, from stored daily
   closes. The 5.26 residual is then computed against them. A hand-written
   basket still wins for a name it contains.
2. **A hot list**: the system reads the market-wide unusual-flow feed, keeps the
   most active names of each session, adds the top few to the live poll on its
   own, and widens the daily-close job to the names it saw, so peers have a
   universe that grows by itself.
3. **A lag record**: every measured (ticker, day) is stored with the peers it
   was measured against, so a flagged lag can be scored on what happened next
   whether or not it was traded.

**Does not build.** No prediction, no score, no alert, no decision. The column
stays display. Nothing in `src/uoa_detector/pipeline/` changes, no combined
score moves, no calibration profile is touched, `board_profile_hash` does not
move. News volume is **not** a selector in this phase (§4).

## 2. Why correlation peers, and why they are point-in-time

The vendor sector is eleven coarse buckets ranked by market cap and is
current-as-of-fetch (5.26 §2). Peers derived from the stored closes use only
history up to the day they are computed, so a peer set written on a date is
replay-safe by construction, and the record in §3.7 stores the set used.

## 3. Frozen decisions

### 3.1 Peer selection (`profiles/peers_v1.yaml`, standalone profile)

For ticker `i` over the last `corr_sessions` sessions of the market proxy's
stored closes:

- daily return of a name on a day is `close(d) / close(prev) - 1` where `prev`
  is the previous proxy session; a day either name lacks is skipped, never
  bridged;
- **excess return** = name's daily return minus the proxy's (`SPY`), so that
  "moves with the market" does not count as "moves with this name";
- Pearson correlation of excess returns with every other name in the closes
  universe that has at least `min_overlap` shared days;
- peers = the `peer_count` highest correlations that are at least `min_corr`,
  ties broken by ticker. Fewer than `min_peers` qualify: the row says so and
  shows no number.

| Key | Value | Why |
|---|---|---|
| `corr_sessions` | 60 | About one quarter; long enough for 40+ shared days |
| `min_overlap` | 40 | A correlation on fewer pairs has a standard error above 0.15 |
| `peer_count` | 5 | The 5.26 basket size |
| `min_corr` | 0.5 | Same-industry names land near 0.5 to 0.7 on excess returns, unrelated names near 0.1 to 0.3 |
| `min_peers` | 3 | The 5.26 `min_members_with_data` |
| `market_proxy` | SPY | The benchmark already stored by the close job |

A missing proxy history means no peers, not raw-return peers.

### 3.2 The residual

Exactly the 5.26 arithmetic (§3.2 to §3.4 there), computed over the ticker and
its selected peers with the 5.26 cutoffs from `profiles/baskets_v1.yaml`
(`window_sessions`, `lag_flag_pct`, `min_basket_move_pct`). The cell says
"akranlarına göre" for automatic peers and "sepete göre" for a hand basket and
names the peers.

### 3.3 The hot list (`profiles/hotlist_v1.yaml`, standalone profile)

- Source: `GET /api/option-trades/flow-alerts` **without** `ticker_symbol`
  (market-wide), newest first, `pages_per_tick` pages of 200. The existing
  `fetch_flow_alerts` refuses an empty ticker list on purpose, so the job
  issues its own bounded request. It never walks unboundedly.
- Per alert: ticker, `total_premium`, ET session date of `created_at`.
  Index and ETF products in `exclude` are ignored.
- Stored per (session date, ticker): the **maximum observed** premium sum and
  alert count across refreshes. The feed window moves, so a sum would
  double-count. The stored value is a lower bound of the session total.
- Refresh: once at `window_start_et` as a daily job (flow left from the last
  session), and on every market-hours tick of the board refresher.
- **Poll list** = `LIVE_TICKERS` ∪ the `poll_top_n` names of the newest session
  with data (premium rank, at least `poll_min_alerts` alerts). `LIVE_TICKERS`
  is never read back or edited; the worker unions the two on each pass.
- **Universe** = names with at least `universe_min_alerts` alerts in any of the
  last `universe_sessions` sessions, at most `universe_cap` by premium. The
  universe gets daily closes.

| Key | Value | Why |
|---|---|---|
| `poll_top_n` | 12 | Poll cost, below |
| `poll_min_alerts` | 2 | One alert is an event, not activity |
| `universe_sessions` | 20 | A month of sessions keeps recently active names |
| `universe_min_alerts` | 2 | As above |
| `universe_cap` | 250 | 250 close requests a day against a 24,000 soft cap |
| `pages_per_tick` | 3 | 600 newest alerts per refresh |

### 3.4 Quota

The per-ticker flow poll is the dominant cost: one request per ticker per 60 s
pass, market hours only, so 12 more names add about 4,700 requests per session.
The hot refresh adds about 3 requests per 5-minute tick (≈ 230 a day), the
close widening up to 250 a day. All below the 24,000 soft cap and the 30,000
hard limit. `poll_top_n` is the lever if the measured daily count says
otherwise.

### 3.5 A name added to the poll mid-session

`UnusualWhalesFlowPollSource` applies its boot-time backfill cutoff to the
tickers present on its first pass only. A name appearing later is emitted with
the same backfill window measured from its own first poll, so adding a name does
not flood the live run with its older alerts.

### 3.6 Rows without flow, closes without waiting

- Hand-basket names get a board row even when they have no print in the shown
  session (flow cells read "kayda değer işlem yok"; the lag cell is the point).
- A daily job `close_catchup` (09:00 ET, trading days) fetches closes for the
  basket members, the hot poll list and the universe that have **none** stored,
  so a name added today is measured today instead of after the 17:00 ET job.
  The 17:00 job covers the same names afterwards.

### 3.7 The lag record

Table `alfa_lag_record`, append-only, one row per (ET date, ticker) of every
`measured` reading, written by the live cycle: source (`hand` or `auto`),
basket id or `auto`, the peers used, window anchors, own and peer returns,
residual, `lagging` and `leading` flags, `version` (`baskets_v1` +
`peers_v1`). Scoring forward is a pure function over stored closes (§3.8).

### 3.8 Scoring a record

`forward_result(record, closes, sessions)`: own return minus the same peers'
mean return from the record's `end` to `sessions` stored sessions later, for 1,
3 and 5 sessions; `None` while the closes do not reach that far. Read-only,
no state. The record is what makes selection bias measurable: flagged rows are
a subset of all measured rows, and both are stored.

## 4. Out of scope, stated so it is not drifted into

- Any prediction claim. H04 tested the nearest claim ("flow leads price") and
  it was REJECTED with the sign reversed (`docs/options-alpha-v1/RESULT_H04.md`).
  The column reports a measured gap.
- News volume as a hot-list selector. No news endpoint is wired in production
  and none was verified live; selecting on an unverified source would be
  invented. Flow-alert activity is the only selector in this version.
- A backtest of the residual. The 2026-05-13 → 2026-09-15 window already carries
  nine declared H-family trials; a study needs its own pre-registration and a
  forward score.
- Editing `LIVE_TICKERS` or any Railway variable.
- Order execution of any kind (CLAUDE.md).

## 5. Acceptance

- Both new profiles load into strict frozen models; a malformed file fails at
  startup, not on a page request.
- Peer selection, the excess-return correlation, the hot ranking and the
  forward score are pure and unit-tested, including self-exclusion, a missing
  proxy, the `min_overlap` and `min_corr` cuts, ties, and the maximum-observed
  upsert.
- A name added to the poll mid-session is not backfilled beyond the window.
- Every failure of the hot refresh, the peers or the record costs its own
  feature and nothing else (the board, the cycle and the live worker run on).
- The gate is green: hermetic `pytest -q`, `mypy --strict src/ webapp/`,
  `ruff check .`, `uv lock --check`.
- Existing tests are not weakened. Two registry tests list the daily jobs by
  name; adding two jobs extends those lists and is flagged in the PR.

## 6. Verification owed after merge

The closeout records, from production: how many names the hot list holds, how
many closes the catch-up job stored, how many board rows carry a peer reading
versus "akran bulunamadı", and the requests used that day.
