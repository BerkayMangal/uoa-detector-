# ThetaData: keep a nightly job, or drop the subscription (Phase 5.2.K2)

Type: decision memo for Berkay. This is not an acceptance contract. The decision
is his; this document changes no account and adds no code.

**Recommendation: take Option B. Cancel or pause the ThetaData subscription now,
do not build the nightly laptop job, and re-subscribe for one month only when a
committed pre-registration or a scheduled Phase 3.5 run names a non-burned window
that needs ThetaData data (earliest foreseeable review: 2026-11-01).**

This confirms the orchestrator's prior (B). The evidence, in one paragraph:
- Nothing live uses ThetaData.
- A nightly job cannot feed the live board cells, for the structural reason in
  K2.
- The one real gain, closing bids for contracts that did not trade, can be
  back-filled in a single batch later.
- The repo already built this exact laptop-to-Postgres pattern once and replaced
  it with Unusual Whales three days later.
- At the repo-recorded price the data bill is a 19% annual hurdle on a
  $10k capital base, with no validated edge to pay for it.

---

## 1. Facts this decision rests on

### 1.1 What ThetaData does in the repo today

- **Nothing live.**
  - The webapp has no ThetaData code path.
  - `THETADATA_*` variables are "Not used by the webapp"
    (`docs/DATA_INTEGRATION.md:554`).
  - The live worker and the gamma loop on Railway call Unusual Whales only
    (`docs/DATA_INTEGRATION.md` §8).
  - The gamma loop exists to remove "the ThetaData-Terminal dependency for the
    gamma board" (`webapp/gamma_live.py:1-7`).
- **Research and backtest only.** Four pieces:
  - Historical `trade_quote` bulk: `scripts/download_bulk.py`.
  - Daily chain snapshots (full-chain open interest plus EOD bid/ask):
    `scripts/download_chain_snapshots.py:1-11, 104-105`.
  - The self-derived providers for the closed v6 track. "Nothing live uses them.
    The track is closed: EDGE REJECTED" (`docs/MODULES.md:763-777`).
  - A WebSocket live source reachable only through the CLI's
    `--feeds thetadata` (`src/uoa_detector/live/factory.py`).
- **Superseded consumer.** M23's stock OHLC moved to UW in Phase 3.3.8, because
  the account lacked STOCK.VALUE (`docs/phase-3.3.8-acceptance.md:28-40`).
- **Owner decision K2.** ThetaData is not wired to the board, because the
  Terminal binds `127.0.0.1:25503` on the laptop and the board runs on Railway
  (`docs/phase-5.2-alfa-board-acceptance.md:72-73`, §8). ThetaData code is a
  5.2 non-goal (same file, line 666).

### 1.2 Research state

"No tradeable edge found" (`docs/INDEX.md:17-24`):
- ThetaData-fed directional confluence (v6): REJECTED.
- Gamma regime and pinning: REJECTED.
- Vol premium: untradeable after costs. At a 10% spread the defined-risk trade
  is t=0.91 (`docs/edge_to_money.md:54-58`).
- UW-fed Track B has never been testable, because UW history covers about
  7 trading days (`docs/phase-3.5.5-status.md:100-110`).

### 1.3 Account state

- Login was rejected on 2026-09-15 with `Invalid credentials`. The tier is
  unknown (`docs/thetadata-capability-probe.md`).
- The last recorded tier is Options PRO, `Max concurrent requests: 8`
  (commit `42f7672`, 2026-05-16).
- The newest local ThetaData datasets were last written on 2026-06-24
  (directory timestamps of `bulk_2024`, `spot_series_2024` and
  `chain_snapshots_2024`). No ThetaData use is recorded after that date.

### 1.4 Price: what the repo records and what it cannot

| Source | Figure |
|---|---|
| `profiles/v5_default.yaml:290` comment | "PRO tier ($160/mo)" |
| commit `42f7672` (2026-05-16) body | "The operator upgraded ThetaData to the PRO tier ($160/mo)" |
| `docs/DATA_INTEGRATION.md:35` and `:329` (older, Phase 3.3.6.1) | Options Pro "~$80/month at the time of writing" |
| `docs/phase-3.3.8-acceptance.md:36` | STOCK.VALUE "$80/mo add-on" (declined) |

**The repo cannot verify:**
- the current price;
- whether billing is still active;
- whether the 2026-09-15 login failure means the subscription lapsed.

This memo uses the latest repo figure, **$160/mo**, and marks every figure
derived from it "at the repo figure".
- At the repo figure: $1,920 a year, or about $480 for July–September 2026,
  months with no recorded use (if billed).
- The stated capital is under $10k (`CLAUDE.md`), and the board default is
  10,000 USD (`docs/alfa-board-decisions.md` P15). $1,920 a year is **19.2% of
  that capital every year**: the return a strategy must make before it has
  paid for its own data, and before option spreads.

### 1.5 Data already on disk (listing only; contents not read)

`/Users/berkay/Documents/uoa-detector-/data/` (whole checkout 15 GB):

| Path | Size | What | Status (`docs/INDEX.md:160-188`) |
|---|---|---|---|
| `historical/bulk/` | 4.7 G | 24 tickers, ThetaData `trade_quote`, 2025-05 → 2026-04 | burned (3.5.5/3.6 replay; 3.6 verdict period) |
| `historical/bulk_2024/` | 3.8 G | 24 tickers, 2024-05 → 2025-04 | used once by Study D; not fresh |
| `historical/bulk_v1_pre_parity/` | 4.5 G | 24 ticker dirs dated 2026-05-31; by its name an earlier bulk pass | not inspected |
| `historical/tier1_reduced/` | 654 M | per-contract layout, 18,033 entries, dated 2026-05-18 | pre-bulk download |
| `historical/dry-run*` (4 dirs) | ~70 M | Phase 3.5.2 dry runs | – |
| `chain_snapshots/` | 93 M | 24 parquet, 251 snapshot dates | burned |
| `chain_snapshots_2024/` | 75 M | 24 parquet | used by Study D |
| `spot_series/`, `spot_series_2024/` | 35 M, 34 M | 24 parquet each | burned / used by Study D |

In `/Users/berkay/uoa-yeni/data/` (this clone), `historical/` holds only
`.manifest.json`, `.download_state.json` and an empty `thetadata/` folder
(2026-09-03), so this clone has no ThetaData parquet. The manifests are an
open item for Berkay (`docs/INDEX.md` §7).

---

## 2. Option A: nightly laptop job pushing to Railway Postgres

### 2.1 Architecture

1. **Scheduler.** A launchd agent with `StartCalendarInterval`, after the
   close; for example 18:30 ET, which is 23:30 UK time.
   - Prefer launchd over cron: launchd runs a missed job once when the laptop
     wakes, while cron skips it.
   - Neither runs a job while the laptop is powered off.
2. **Terminal up.**
   - Start `ThetaTerminalv3.jar` with a clean environment
     (`docs/thetadata-capability-probe.md` §3).
   - Wait for `127.0.0.1:25503` with a timeout.
   - Exit non-zero on `Invalid credentials`.
3. **Pull**, per board ticker, for session date D:
   - Chain snapshot: `GET /v3/option/history/eod?symbol=T&expiration=*&date=D`
     (closing bid/ask per contract) and
     `GET /v3/option/history/open_interest?symbol=T&expiration=*&date=D`. These
     are the endpoints `scripts/download_chain_snapshots.py:58-65, 104-105`
     already uses, there with `start_date`/`end_date`.
   - Greeks: `GET /v3/option/snapshot/greeks/first_order` (Standard) or
     `/greeks/all` (Pro) with `expiration=*`. Run after the close, these return
     the closing state. They are not used anywhere in the repo yet.
4. **Push.**
   - Write with `DATABASE_URL` into a new append-only table.
   - The key is (ticker, session date, contract), so a re-run is idempotent.
   - Write one heartbeat row per night.
5. **Stop the Terminal.**
6. **Board side.** A reader shows the data's session date. It renders
   `veri yok` when the heartbeat is missing, and never calls the data live
   (`docs/alfa-board-decisions.md` P16).

**Scope.** Option A needs:
- a new numbered phase with its own acceptance doc (`docs/INDEX.md` §7 rules):
  job, table, reader, tests and monitoring;
- the 5.1 prerequisite "Alembic for webapp tables with a prod stamp (additive
  only)", which is not built yet (`docs/INDEX.md:210`).

**Precedent: this architecture already existed and was removed.**
- `scripts/gamma_snapshot.py:1-11`, added in Phase 4.10 (`d67ee83`,
  2026-06-19), read ThetaData chain snapshots on the laptop and upserted
  `gamma_regime` through `DATABASE_URL`.
- Phase 4.19 (`60ef351`, 2026-06-22) replaced it three days later with a UW loop
  running on Railway (`webapp/gamma_live.py:1-7`).
- The footer at `webapp/templates/gamma.html:59` still describes the old job.

### 2.2 Security of the production database credential on the laptop

**What leaves Railway.**
- `DATABASE_URL` is the full connection string for production Postgres, the one
  the webapp and worker use to read and write `signal`, `trade`,
  `gamma_regime` and the board tables (`docs/DATA_INTEGRATION.md:546`).
- A Railway-generated connection string normally carries the database's owner
  role. This was not checked for this database.

**What a laptop copy exposes.**
- A stolen laptop, malware, a synced dotfile or leaked shell history could then
  read or wipe the live journal and the append-only board tables.
- launchd and cron do not read the macOS Keychain on their own, so the easy
  setup leaves the URL in plain text in a plist or `.env`.

**Minimum mitigations if A is chosen**, each extra work and a new failure point:
- a dedicated Postgres role with `INSERT` on one staging table only, created by
  an admin;
- the URL kept in the Keychain and read by the script;
- TLS required;
- a separate credential from the webapp's, so rotating one does not break the
  other;
- the job never runs DDL.

**Rotation.** The production database password has already rotated once; the
live site served an empty fallback until the URL was re-copied (session notes
from the 2026-09-14 recovery; not in a repo doc). Every future rotation would
silently stop a laptop job unless the job alerts.

### 2.3 Laptop dependency and failure modes

| Failure | Effect | Needed guard |
|---|---|---|
| Laptop asleep at run time | launchd runs the job once on wake; cron skips it | heartbeat row; the board shows the data's date |
| Laptop off, travelling, or offline | that night is missing | heartbeat; `veri yok` on the board |
| Terminal login fails (the 2026-09-15 state) | zero rows | detect the log line and exit non-zero |
| Terminal self-updates: the jar on disk is dated 2026-05-12, the Terminal that ran is build `20260819` | log lines, ports or behaviour change without notice | pin checks on the port and the login line |
| A manually started Terminal already holds port 25503 (Berkay runs one for research) | the job fails or reuses the wrong session | detect an existing process and stop |
| EOD data not yet published at run time (timing unverified) | HTTP 472 "no data" for every ticker | retry window |
| Production DB password rotated | all writes fail | alert on connection failure |
| The webapp deploys a schema change while the laptop runs an older checkout | writes fail or land in the wrong columns | one migration owner (5.1 Alembic); job version check |
| Partial night: some tickers written | uneven board | idempotent keys and per-ticker status |
| Daylight saving: launchd uses local UK time; the UK and US change clocks on different weekends | the ET run time shifts by one hour for about 1 week in autumn and about 3 weeks in spring | compute the run time in ET inside the job |

### 2.4 What the board gains over the UW data it already has

| Board need | UW today | What a nightly ThetaData job adds |
|---|---|---|
| Live NBBO, spread and round-trip cost (A2) | `option-contracts` per ticker, with our `fetched_at` age | **Nothing.** Nightly data is end-of-day; the chip needs the current session. K2's structural reason stands. |
| Quote age, live book, bid/ask size (P16) | not available over REST (P16) | **Nothing live.** ThetaData quotes carry sizes and timestamps, but only a Terminal on the same host delivers them intraday. |
| Dealer gamma (B5) | UW `spot-exposures` (P3) | A second, self-derived GEX from a closed track (`docs/MODULES.md:763-777`; gamma studies REJECTED) |
| IV and greeks per contract | UW rows carry IV and greeks, but they are null when the contract did not trade that day (UW probe 2026-09-15, session scratchpad) | Closing greeks for untraded contracts. Display only; scoring may not change. |
| Next-day OI confirmation (B4) | UW start-of-day OI (P10) | the same information from another vendor |
| C2 secondary outcome: the dominant contract's bid on the horizon day | UW `/api/option-contract/{occ}/historic`, `veri yok` on zero-volume days (`docs/phase-5.2-decision-cards-acceptance.md:75-78`) | **A real gain:** the closing bid for contracts that did not trade. That EOD rows exist for zero-volume contracts is unverified. It does not need a nightly job; a later batch can back-fill it. |
| A fresh window for a future study | none; UW history is about 7 trading days | nothing a later one-month download cannot fetch. ThetaData serves history on demand, and the whole 2024 panel's last writes fall on the evening of 2026-06-24 (18:53–21:46, directory timestamps). |

Net: everything live stays on UW, and the one real gain can wait for a batch.

### 2.5 Bandwidth

- **No 50–200 GB download.** Option A pulls end-of-day chain rows only. The owner
  spec forbids starting the historical download, which
  `docs/phase-3.5-acceptance.md:162` sizes at 50–200 GB of free disk.
- **Size estimate from local files.**
  - `chain_snapshots/` is 93 MB for 24 tickers × 251 dates, about 15 KB of
    compressed parquet per ticker-day.
  - The wire size of a full-chain CSV/JSON response was never measured. Even at
    100 times that (1.5 MB per ticker-day), 10 tickers × 21 sessions is about
    0.3 GB a month.
- **Calls.** 2–4 per ticker per night, so about 20–40 calls for 10 tickers.
  That is far below the recorded ~25 requests per second
  (`profiles/v5_default.yaml:290`).
- **Postgres growth is the real cost, not bandwidth.**
  - Full chains run to thousands of contracts per liquid ticker (the UW probe
    saw 4,052 rows in one NVDA chain).
  - 10 tickers every night means tens of thousands of rows a night.
  - Retention and a dedicated Postgres are still open registry items
    (`docs/INDEX.md:219`).

---

## 3. Option B: drop or pause until a pre-registered need

### 3.1 Saved

- The subscription: $160 a month at the repo figure ($1,920 a year).
- No new phase to build, test and maintain.
- No production database credential on a laptop.

### 3.2 Lost

- **Ad-hoc access.** No ThetaData queries (history, snapshots, greeks) until
  re-subscribed; a new research question waits one sign-up.
- **ThetaData code paths.** The ThetaData smoke tests and the CLI WebSocket
  source cannot run against a live account. The smoke tests already skip
  without the key (`tests/integration/test_thetadata_smoke.py:66-69`), so CI
  is unaffected.
- **Nothing live is lost.** The board and the worker do not use ThetaData.
- **Unverified; check on the account page before cancelling:**
  - whether pausing exists;
  - whether a returning account gets the same tier and price;
  - whether the licence lets you keep data downloaded while subscribed. The repo
    does not record ThetaData's terms.

### 3.3 Burned windows: local panels do not replace a subscription for new work

**The rule.** A new study needs a fresh, non-overlapping window, and registry
5.20+ allows non-burned windows only (`docs/INDEX.md:162-163`,
`docs/next_studies.md:21-26`).

**The local panels.**
- 2025-05-01 → 2026-04-30 (`bulk/`, `chain_snapshots/`, `spot_series/`):
  burned.
- 2024-05-01 → 2025-04-30 (`bulk_2024/`, `chain_snapshots_2024/`,
  `spot_series_2024/`): used by the single pre-registered Study D run; not fresh
  for a later study.

**The consequence.** Every future pre-registered study needs a new download
whatever happens to the subscription. Keeping the subscription does not make
the local panels reusable, and dropping it does not destroy anything a new
study could use.

### 3.4 Trigger to re-subscribe

Re-subscribe when any one of these is true. Each trigger is a committed document,
never a hunch.

- **T1. A committed pre-registration** (`docs/preregister_E.md` or later,
  registry 5.20+) names a non-burned window with ThetaData as a source.
  - The earliest foreseeable case is Study D's registered forward window,
    2026-05-01 → present.
  - Its stated revisit point is "once ≥6 months of forward data exist
    (~Nov 2026+)" (`docs/preregister_D.md:67-71`), i.e. **2026-11-01**.
- **T2. UW full history is obtained** (B0, `docs/phase-3.5.5-status.md:114`) and
  a Phase 3.5 Track B run is scheduled. That window's trade tape comes from
  ThetaData.
- **T3. Berkay approves a board feature** whose acceptance doc requires data UW
  cannot supply, for example C2 option bids on zero-volume days. It is run as a
  scheduled batch backfill, not a nightly job.

**On a trigger:**
1. Re-subscribe for one month, at the tier the study needs (probe doc §5 table).
2. Run `docs/thetadata-capability-probe.md` §3–§4 first.
3. Download only the registered window.
4. Cancel at the end of the month.
5. Record the tier, price and dates in the study's own document.

---

## 4. Cost/benefit

Money figures use the repo-recorded $160/mo (unverified).

| | Option A: nightly laptop job | Option B: drop until triggered |
|---|---|---|
| Recurring data cost | $160/mo, $1,920/yr | $0; about $160 per triggered study month |
| Build cost | a new phase: acceptance doc, job, table, reader, tests, monitoring; needs 5.1 Alembic first | none |
| Running cost | nightly Terminal login, launchd, credential rotation, daylight-saving drift, laptop awake and online | none |
| Security | a production database write credential on a laptop | nothing added |
| Live board gain | none: end-of-day data only (K2) | none lost: ThetaData is not live today |
| C2 outcome bids on untraded days | filled nightly (EOD rows for zero-volume contracts unverified) | back-filled in one batch under T3 |
| Research data | a nightly archive of data the vendor already keeps | the same data, fetched on demand for a registered window |
| Precedent | already tried as `gamma_snapshot.py`; replaced by UW in 3 days | – |
| Reversibility | hard: a job, a table and data semantics to maintain or remove | easy: re-subscribe |
| Edge context | a 19.2%/yr hurdle on $10k with no validated edge (`docs/INDEX.md:17-24`) | no hurdle |

## 5. What would change this recommendation

- **Already paid for a long term** (for example an annual plan): cancelling
  saves nothing now. Still do not build A; use the remaining term for a download
  only if a T1–T3 document exists.
- **A pre-registration is committed and needs data within 1–2 months:** keep the
  subscription through that download, then cancel.
- **The board truly needs a live book** (P16) and Berkay wants ThetaData to
  supply it: that means running Theta Terminal on a cloud host next to the
  board. This memo does not evaluate that option. It needs its own phase, a check
  of the licence terms, and a hosting change, and ThetaData code is out of scope
  for 5.2.

## 6. Berkay's actions

1. Log in at thetadata.net and note the plan, price, billing state, renewal date
   and the data-retention terms.
2. If you are billed and no T1–T3 document exists: cancel or pause.
3. If you are not billed (lapsed): nothing to do; that also explains the
   2026-09-15 login failure.
4. Put 2026-11-01 on the calendar: the Study D forward-window review
   (`docs/preregister_D.md:67-71`).
5. When a trigger fires: follow `docs/thetadata-capability-probe.md` §3–§5, then
   download only the registered window.
