# Phase 5.2 — Alfa Board (acceptance contract)

Status: **FROZEN** (per D1) from its first commit. Owner: Berkay. Author: Claude (Opus 5).

Approval:
- Berkay ordered this phase on 2026-09-15 with a complete written spec (items
  A1–D3, rules §1, decisions K1–K4) and the instruction to build it
  autonomously without stopping for questions. That message is the approval
  CLAUDE.md requires.
- This document carries his spec into the repo and pins the design choices
  made to implement it.
- Ambiguities resolved during implementation are recorded in
  `docs/alfa-board-decisions.md` (decision / rationale / how to undo).
  They never edit this contract.
- FAZ C (decision cards, pass ledger, fill capture) has its own contract:
  `docs/phase-5.2-decision-cards-acceptance.md`.

---

## 1. Objective

Turn the live dashboard from per-print signal cards into a decision board that is
honest about cost, counter-evidence and missing data. The board must be able to
say "nothing clean today". It must record what the owner passed on as carefully
as what he took, so his own edge can be measured forward.

What sets it apart (owner spec §2, in priority order):
1. **Cost is first-class.** Spread %, round-trip dollars and 1 lot as a
   percentage of capital are on the row face, and the tradability gate is on by
   default. The project's own thesis died on cost: iron fly t=0.91 at a 10%
   spread, negative at 20% (`docs/edge_to_money.md`).
2. **A mandatory counter-argument** on every row.
3. **Honest unknowns.** Unknown is never painted as clean.
4. **Multi-source alignment.** Six counted families; delayed families sit
   apart and are not counted.
5. **Shadow ledger.** Passes are recorded counterfactually.
6. **A product that can kill its own thesis**, and says so on screen.

The board is decision support. It never opens, routes or sizes an order for
execution.

## 2. Binding honesty rules (owner spec §1, verbatim UI strings)

Each rule is a testable requirement. `R-*` ids are referenced by the render
tests (`tests/unit/test_board_honesty.py`) and the closeout. Turkish strings
below are UI copy and must render byte-for-byte.

| Id | Requirement | Enforced by |
|---|---|---|
| R-EV1 | No number is presented as a probability, expected value or odds. The evidence word counts independent families pointing the same way. Its hover text is exactly `Kâr olasılığı DEĞİL.` | render test on every evidence word |
| R-EV2 | The 0-1 combined score never appears on the row face, on a chip, or as a sort key. It appears only in the row's detail audit block. | render test: score absent outside the audit block; sort-key unit test |
| R-UN1 | An unknown family (timeout, error, no data) or an out-of-scope family renders dashed and dimmed, is excluded from counts, and says `bilgi yok, temiz demek değil`. | view-model and render tests |
| R-UN2 | With 3 or more unknown families, the label `Güçlü` is forbidden at render level. The cutoff lives in the profile. | render guard plus test |
| R-CA1 | Every row carries a counter-argument clause starting `AMA`. If none is found, the row renders `Bariz bir karşı argüman bulunamadı — bu bir onay değildir` followed by the list of what was checked. | narrative unit test; render test on every row |
| R-CA2 | The bear column has the same width and typography as the bull column. | template structure test (same classes) |
| R-CO1 | Cost always uses real prices: entry at ask, exit at bid, both legs, commission included. Mid is never a headline. | cost unit tests; render test (no "mid" headline) |
| R-CO2 | Every quote shows its age (e.g. `41 sn önce`). The phrase `canlı alınabilir fiyat` never appears. | render test |
| R-DL1 | Delayed families (Kongre, İçeriden, Short/FTD) render with filing date, delay and outcome, and are never counted as evidence. | view-model test (counts exclude delayed) |
| R-IV1 | Wherever IV richness is shown, the unsuppressible sentence renders: `Bu 'vol sat' demek DEĞİLDİR — o tez test edildi, maliyet ve örnek-dışı testten sonra ayakta kalmadı.` | render test on every IV-rich surface |
| R-WD1 | Generated text never contains the forbidden words `al`, `öneri`, `fırsat`, `en iyi`, `garanti`. Word-boundary and stem matching; `Alabileceklerimi göster` (the owner-named gate label) is not generated text. | lint test over the frozen dictionaries plus rendered board HTML |
| R-EM1 | `Bugün temiz aday yok` is a first-class board state, rendered when no row qualifies as a clean candidate (definition in §4). | view-model and render tests |

## 3. Scope and non-negotiables (owner spec §1 safety, §4 K1–K4)

- **Profile.** No existing threshold changes (D4/D8). Every new numeric cutoff
  goes into a new `board:` profile section with an explicit value in
  `profiles/v5_default.yaml`.
- **Evidence families (K1).** The counted families are Akış, Dealer gamma,
  Karanlık havuz, Sektör, Fiyat teyidi and Açık pozisyon. Which families count
  is configured in the profile (`board.evidence.counted_families`).
  Kongre / İçeriden / Short are in the delayed bucket.
- **ThetaData (K2).** Not wired to the live board. Probe and documents only:
  `docs/thetadata-capability-probe.md` and `docs/thetadata-decision.md`.
- **Language (K3).** UI is Turkish; code, commits and docs are English.
- **Ambiguity (K4).** Take the most honest, most reversible option and record
  it in `docs/alfa-board-decisions.md`.
- **No UW calls at render time.** Page renders read Postgres only; background
  refresher loops populate board tables within a UW request budget.
- **No execution.** Log and pass actions write records; nothing sends, routes
  or places an order.
- **No frozen-doc edits (D1).** No `.env` or secret in git.
- **Live integration tests.** Every new UW endpoint has one that runs with the
  key and asserts real data.
- **Deploy per FAZ.** Only green; verify the live URL; roll back if broken.

## 4. Architecture (pinned)

### 4.1 Data flow

Page renders read Postgres only. No render and no board POST makes an Unusual
Whales call.

- **Live worker.** `webapp/worker.py` keeps its path and still writes `signal`
  rows. A new `DecisionRecordWriter` attached to its `Pipeline` also writes:
  - one row per (event, stage) into `alfa_stage_telemetry`;
  - one row per event into `alfa_print_meta`: fill_side, the OCC option chain,
    and the degraded-provider flags.
  Today the `SignalDecisionRecord` is built and discarded
  (`src/uoa_detector/pipeline/orchestrator.py:250-258`,
  `webapp/worker.py:160-164`).
- **Board refresher.** `webapp/board/refresher.py` runs under `_supervise` in
  the app lifespan.
  - It uses **one long-lived** `UnusualWhalesClient`, so its token bucket,
    circuit breaker and caches survive between cycles.
  - It fills the `alfa_*` tables on the cadences in §4.4.
  - Guards follow `flow_poll`: RTH gate; 1800 s sleep on
    `UnusualWhalesDailyLimitError`; skip the cycle while the breaker is open.
  - It pauses non-critical fetches when the client's last seen
    `x-uw-daily-req-count` reaches `refresh.daily_request_soft_cap`. The count
    is exposed read-only by an additive client attribute; client behaviour does
    not change.
- **Daily jobs** run in the same refresher on profile clock times (ET):
  - pre-market: T+1 open-interest confirmation (B4) and ticker info;
  - post-close: delayed families (D1–D3), ETF holdings (B6), daily closes,
    counterfactual outcomes (FAZ C).
- **Render.** `GET /alfa` (and `GET /` after A7) renders the board from
  `signal`, `alfa_*` and `trade`. Aggregation covers the whole selected run, not
  a page capped by score. Parsed rows are cached in process, keyed by
  `(run_id, event_id)`; only new rows are parsed on each render. Render budget:
  p95 ≤ 1.5 s at 2,000 signals in the run (test).

### 4.2 New tables

- Every new table has the `alfa_` prefix. The production Postgres is shared
  with another service and already holds generic names such as `signal` and
  `trade`.
- The tables use their own `DeclarativeBase` and `create_all`.
- No existing table, column or model changes. `StoredSignal`, `SignalRow` and
  `TradeRow` stay byte-identical, so a rollback to Phase 5.0 or to
  `phase-3-final` stays data-safe.

| Table | Key | Kind | Filled by |
|---|---|---|---|
| `alfa_stage_telemetry` | (run_id, event_id, stage_name) | append-only; replay-idempotent (skip an existing key) | worker writer: branch, metadata JSON, degraded flag |
| `alfa_print_meta` | (run_id, event_id) | append-only; replay-idempotent | worker writer: fill_side, option_chain, print time |
| `alfa_quote` | option_symbol | rebuildable (upsert) | refresher |
| `alfa_contract_depth` | option_symbol | rebuildable (upsert) | refresher, top-K contracts only |
| `alfa_atm` | (ticker, expiry) | rebuildable (upsert) | refresher |
| `alfa_net_prem` | (ticker, trade_date) | rebuildable (upsert) | refresher: cumulative day totals plus per-minute rows for the since-print sums |
| `alfa_regime` | (source, fetched_at) | append-only history (keeps tripwire persistence countable) | refresher |
| `alfa_ticker_info` | ticker | rebuildable (upsert) | daily job: issue_type, sector |
| `alfa_catalyst` | (ticker, kind, when, title) | rebuildable per day | daily job |
| `alfa_oi_confirm` | (option_symbol, trade_date) | append-only; status advances from `bekliyor` to final once | daily job |
| `alfa_delayed` | (ticker, family, dedupe_key) | append-only | daily job |
| `alfa_etf_holding` | (etf, ticker, updated) | rebuildable per snapshot | daily job |
| `alfa_daily_close` | (ticker, day) | append-only | daily job |
| `alfa_decision_card`, `alfa_fill`, `alfa_outcome` | see `docs/phase-5.2-decision-cards-acceptance.md` | append-only | POST routes, daily job |

Rebuildable tables hold refreshed market data and can be refilled at any
time. Append-only tables hold forward evidence and are never dropped or
rewritten. `reset()`-style drop and recreate is forbidden for them.

### 4.3 Board profile

- **File and model.** Every new board cutoff lives in `profiles/board_v1.yaml`,
  loaded into a strict `BoardSettings` model (`webapp/board/settings.py`).
  - This file is deliberately separate from `CalibrationProfile`.
    `content_hash` covers the full calibration model
    (`src/uoa_detector/calibration/profile.py:1562-1569`). A `board:` section
    there would change the hash of every profile, including the burned
    `v5_gamma_squeeze` and `v6_thetadata_confluence` hashes that trace recorded
    verdicts.
- **Spread cutoff.** The İŞLENMEZ cutoff is **not** duplicated. As the owner
  specified in A2, the board reads `penalty_triggers.spread_pct_threshold`
  (15.0, percent of mid) from the live calibration profile
  `profiles/v5_default.yaml`, and never modifies it.
- **Counted families (K1)** are listed under `evidence.counted_families`.
  Removing a family there removes it from the count without a code change.
- **Units.** Every percent key is in percent units, matching
  `spread_pct_threshold`.
- **Owner-only values.** The owner has not stated capital, dollar-R or
  commission. The file pins disclosed defaults: capital 10,000 USD (his stated
  upper bound), R 100 USD, commission 0.65 USD per contract (the cost model of
  `docs/edge_to_money.md`). While `sizing.values_confirmed_by_owner` is
  `false`, every size and cost cell shows `(varsayılan değer)`.

### 4.4 UW request budget

Per day, N = 10 live tickers. The live key had used about 4,100 of its 30,000
daily requests by 15:30Z on 2026-09-15, with the live worker and gamma loop
running.

| Data | Endpoint (live-probed 2026-09-15) | Cadence | Requests/day |
|---|---|---|---|
| NBBO for signal contracts and open journal legs | `/api/stock/{t}/option-contracts?option_symbol[]=…` | 300 s, RTH | ≈ 780 |
| Exit depth and last-print quote time (top-K rows) | `/api/option-contract/{occ}/flow?limit=1` | 300 s, RTH, K from profile | ≈ 780 (K = 10) |
| ATM straddle | `/api/stock/{t}/atm-chains?expirations[]=…` (plus `expiry-breakdown` once a day) | 300 s, RTH | ≈ 790 |
| Net premium tape | `/api/stock/{t}/net-prem-ticks` | 300 s, RTH | ≈ 780 |
| Regime | `market-tide`; SPY/QQQ `spot-exposures`; SPY/QQQ `gex-levels?source=oi`; SPY and VIX `volatility/term-structure` | 300 s, RTH | ≈ 550 |
| Ticker info | `/api/stock/{t}/info` | daily | ≈ 10 |
| Catalysts | `/api/earnings/{t}`; `/api/market/fda-calendar?ticker=&target_date_min=`; `/api/market/economic-calendar` | daily | ≈ 21 |
| T+1 open-interest confirmation | `/api/option-contract/{occ}/historic?limit=5` | daily, flagged dominant contracts | ≤ 60 |
| Delayed families | congress `recent-trades`; `insider/transactions`; `shorts/{t}/interest-float/v2`; `shorts/{t}/ftds` | daily | ≈ 40 |
| ETF holdings | `/api/etfs/{etf}/holdings`, the profile's focused list | daily | ≈ 12 |
| Daily closes | `/api/stock/{t}/ohlc/1d` (SPY included) | daily | ≈ 11 |
| SPY/QQQ one-year gamma percentile | `/api/stock/{t}/greek-exposure` | daily | 2 |

Total: ≈ 3,800 requests/day. Cadences, K and the soft cap live in the board
profile.

## 5. FAZ A — make the board readable

### A1. Per-ticker aggregation

- **Rows.** One row per (ticker, direction) over the whole selected run. Each
  row carries:
  - total premium, print count, distinct contracts;
  - the dominant contract: the largest summed premium per (strike, expiry,
    type).
- **Direction is side-aware.**
  - The worker writer records `fill_side` in `alfa_print_meta`. `at_ask` counts
    as bought: call → `yukarı`, put → `aşağı`. `at_bid` counts as sold: call →
    `aşağı`, put → `yukarı`.
  - A print with unknown or mid side, or a legacy row with no meta, falls back
    to option type (call → `yukarı`, put → `aşağı`). It carries the marker
    `yön opsiyon tipinden (alım/satım tarafı bilinmiyor)`.
  - Side-aware and fallback prints never merge silently. The row shows how
    many prints used each.
- **Concentration** = the top strike's premium divided by the row's total
  premium, in percent.
- **Dominance** = row premium divided by (row premium + the same ticker's
  opposite-direction premium), in percent.
- **Position read** (cutoffs in the board profile):
  - `kasıtlı pozisyon`: concentration ≥
    `aggregation.intentional_min_top_strike_share_pct`;
  - `dağınık envanter`: concentration ≤
    `aggregation.scattered_max_top_strike_share_pct` with at least
    `aggregation.min_strikes_for_scattered` distinct strikes;
  - `karışık`: anything else.
- **Detail view.** Lists every constituent print.

### A2. Tradability chip and cost gate

- **Quotes.** The refresher stores `nbbo_bid`, `nbbo_ask`, `last_price`,
  `volume`, `open_interest` and `last_tape_time` per contract, with our own
  `fetched_at`. Requested and returned symbols are diffed; a symbol UW does not
  return is recorded as `yok`.
- **Top-K depth.** For the top-K rows, `/flow?limit=1` adds
  `nbbo_bid_size`, `nbbo_ask_size` and `nbbo_*_time`. These are as of the last
  print, and the UI says `son işlem anında`.
- **Row face** (dominant contract):
  - spread % of mid (formula of `penalties.py:62-65`);
  - round-trip dollars for 1 contract = (ask − bid) × 100 + 2 × commission per
    contract. The two legs are the entry at ask and the exit at bid;
  - 1 contract as % of capital = ask × 100 / capital;
  - exit depth = bid size, or `bilinmiyor`;
  - quote age: `kotasyon {n} sn önce alındı`, plus
    `son işlem {m} dk önce` when known.
  - The row never shows mid as a headline, and never says
    `canlı alınabilir fiyat`.
- **States:**
  - `İŞLENİR`: spread % ≤ `tradability.max_tradable_spread_pct`, and bid size
    ≥ `tradability.min_exit_bid_size` or unknown.
  - `DAR`: spread % above that and ≤ `penalty_triggers.spread_pct_threshold`,
    or bid size < `tradability.min_exit_bid_size`.
  - `İŞLENMEZ`: spread % > `penalty_triggers.spread_pct_threshold`, or
    bid = 0.
  - `kotasyon yok`: NBBO null (the contract has not traded today; probe fact),
    quote older than `tradability.max_quote_age_seconds`, or no quote row.
    This is an unknown state, never İŞLENMEZ.
- **Gate** `Alabileceklerimi göster`, on by default:
  - The main list shows İŞLENİR and DAR rows.
  - `kotasyon yok` and `İŞLENMEZ` rows move into their own sections below,
    each with its reason (e.g. `SMCI %17 makas`). They are never hidden or
    deleted.
  - Turning the gate off shows every row in one list with its chip.

### A3. Unknown is not clean

- **States.** Each counted family gets one state per the §9 table: `lehte`,
  `aleyhte`, `bilinmiyor` or `kapsam-dışı`. The state comes from the telemetry
  of the dominant contract's largest print.
- **Orientation.** Stage results are relative to option type. When the
  side-aware direction reverses the option-type direction (a sold option),
  `lehte` and `aleyhte` swap.
- **Measured neutral.** A result that was measured but points neither way
  renders `nötr (ölçüldü, yön göstermiyor)`. It enters no count. This is a
  display variant of the four states, recorded in
  `docs/alfa-board-decisions.md`.
- **Degraded calls.** The worker writer diffs the public `Degrading*.errors`
  counters around each event; events are processed sequentially. A stage
  whose provider degraded on that event is `bilinmiyor`, even when its branch
  string reads as no-data.
- **Legacy rows** (written before telemetry existed):
  - only the unambiguous values in §9 map to `lehte`/`aleyhte`;
  - everything else is `bilinmiyor`, labelled `telemetri yok (eski satır)`.
- **Akış (Flow).** Source: the independent net-premium tape (`alfa_net_prem`),
  not the print that created the row.
  - direction net aggressor premium > `evidence.flow_net_premium_deadband_usd`
    → `lehte`;
  - opposite side above the dead-band → `aleyhte`;
  - inside the dead-band → `nötr`;
  - no tape row, or a stale one → `bilinmiyor`.
- **Açık pozisyon (Open interest).** Source: the T+1 confirmation only (B4).
  - M27's print-time reading measures the previous session's open-interest
    change, not the flagged print (open interest is start-of-day, probe
    2026-09-15). It appears only in the audit block, labelled
    `önceki seans OI değişimi (bu baskı değil)`.
  - Until B4 ships, and before T+1 is published, the family is
    `bilinmiyor (T+1 bekleniyor)`.
- **Render.** Unknown and out-of-scope chips are dashed and dimmed, and carry
  `bilgi yok, temiz demek değil`. With
  `unknown ≥ evidence.max_unknown_for_strong + 1` (3 with the pinned values),
  the render guard refuses the `Güçlü` label (R-UN2).

### A4. Evidence strip and derived evidence word

- **Strip.** The six counted families, in profile order, as signed chips.
- **Evidence word.** `{L} lehte · {A} aleyhte · {U} bilinmiyor`, with hover
  text `Kâr olasılığı DEĞİL.`
- **Strength label** (thresholds in the profile):
  - `Güçlü`: L ≥ `strong_min_supporting`, A = 0 and
    U ≤ `max_unknown_for_strong`;
  - `Orta`: L ≥ `moderate_min_supporting` and A ≤ L;
  - `Zayıf`: otherwise.
- **The combined score is removed** from the row face, the chips, sorting,
  filters and the default order. It appears only in the row's detail
  `Denetim` block as `Birleşik skor (denetim, sınırsız ölçek): 0.xx`, next to
  its inputs. The score is not clamped, so it is never shown as a 0–1 gauge.
- **Row order inside a section:** L desc, A asc, U asc, total premium desc.

### A5. Reason sentence and mandatory counter-argument

- **Frozen templates.** Sentences come only from frozen template dictionaries
  in `webapp/board/narrative.py`. Placeholders take row values; there is no
  free text.
- **Reason sentence.** Built from the `lehte` families and the position read,
  e.g. `Neden: 3 bağımsız kaynak yukarıyı gösteriyor; prim tek strike'ta
  toplanmış (kasıtlı pozisyon).`
- **Counter-argument**, starting with `AMA`. It names the most material
  negative, in this priority order:
  1. İŞLENMEZ or DAR cost;
  2. `aleyhte` families;
  3. chase verdict `geç kaldın` (B3 onwards);
  4. a catalyst inside the window (B4 onwards);
  5. two or more unknown families;
  6. penalty ledger items.
  If none applies, the row renders exactly
  `Bariz bir karşı argüman bulunamadı — bu bir onay değildir`, followed by
  `Bakılanlar: …` listing the checks performed.
- **Guard.** A unit test passes every template through `ensure_clean` (R-WD1).

### A6. Penalty ledger

The detail block lists the eight Module 36 penalties
(`src/uoa_detector/scoring/penalties.py`) plus the M24 post-earnings IV
adjustment, with Turkish names from a frozen dictionary. Each entry has one
status:

- `uygulandı`: present in `penalties_applied`, with its value and reason.
- `uygulanmadı`: measured on the live path and did not fire.
- `canlı yolda ölçülmüyor`: its input is never set on the live path. Evidence:
  - `iv_rank_high`: `apply(event)` is called without iv_rank
    (`orchestrator.py:213`).
  - `post_gap_move`, `post_event`, `isolated_print`,
    `flow_contradicts_price`: no production setter; only
    `sources/scenarios.py` sets them.
  - `next_day_oi_failed`: the field is not updatable.
  - `wide_spread`: live prints carry bid = ask = price
    (`flow_poll.py:190-192`). The A2 chip is the honest cost signal.
- `kaydedilmedi`: the M24 adjustment is folded into the pre-penalty score and
  not persisted separately.

Numbers come from the profile that wrote the row
(`SignalRow.profile_content_hash`). This answers "why 0.43" with named defects
and named blind spots, not with a number.

### A7. Switch

In one commit:
- `GET /` renders the Alfa Board.
- The per-print cards and their score, sort and filter controls are removed.
- The vol board stays as a section, with R-IV1.

Existing dashboard tests that assert the removed copy change in this commit,
and are listed in its body (D10).

## 6. FAZ B — sharpen the decision

### B1. Position size

- **Cell.** 1 contract = ask × 100 in dollars, and as % of
  `sizing.capital_usd`.
- **Risk bucket line.** `Profil risk kovası: {bucket}, max_r {x} → {x} × R =
  ${y} → {n} lot`, where n = floor(y / (ask × 100 + commission)).
- **Disclosure.** max_r comes from the label, which the combined score drives.
  The audit block says so.
- **Unknowns.** A null ask gives `bilinmiyor`. While values are not confirmed,
  the cell shows `(varsayılan değer)` (§4.3).

### B2. Required move vs expected move

- **Required move** for the dominant contract, entered at the current ask,
  against the current spot (`alfa_atm.stock_price`):
  - call: (strike + ask) / spot − 1;
  - put: 1 − (strike − ask) / spot.
- **Expected move** = the ATM straddle mid for the same expiry, or the nearest
  one: (call mid + put mid) / spot. The ATM strike's offset from spot is
  disclosed.
- **Fallback.** If there is no ATM row, a labelled
  `IV tahmini (straddle değil)` fallback uses `atm_iv · sqrt(days/365)`.
- **Wording.** `Başabaş için %X gerekir · ATM straddle bu vadeye %Y fiyatlıyor`.
  No probability is stated.

### B3. Chase verdict

One line. It compares the print price (`StoredSignal.option_price`) with the
current **ask**, because an entry now would happen at ask, and adds the
underlying's move since the print (spot at print = moneyness × strike):
`baskı $4.05 → şimdi $4.20 (ask), %4 yukarıda; hisse baskıdan beri %+0.6`.

Verdict bands (profile) on the option-price change:

| Change | Verdict |
|---|---|
| ≤ `chase.reasonable_max_pct` | `hâlâ makul` |
| between the two cutoffs | `dikkat` |
| ≥ `chase.late_min_pct` | `geç kaldın` |
| no quote | `kotasyon yok` |

The net premium in the row's direction since the print (`alfa_net_prem`) is
context only: `akış baskıdan beri sürüyor` / `döndü` / `bilinmiyor`.

### B4. Opening or closing, and catalyst in window

- **Opening/closing tri-state** for the dominant contract, from
  `alfa_oi_confirm`. ΔOI = OI(T+1) − OI(T), where OI(T+1) is the row published
  pre-market on T+1.
  - `açılış (T+1 OI teyitli)`: ΔOI ≥
    `opening_closing.confirm_open_min_ratio` × flagged size.
  - `kapanış (T+1 OI düştü)`: ΔOI ≤ `confirm_close_max_ratio` × flagged size.
  - `henüz doğrulanmadı`: T+1 not yet published, or ΔOI falls between the two
    cutoffs.
  - `kapsam-dışı (T+1'den önce vade)`: the contract expires before T+1.
  - The Açık pozisyon evidence family (A3) takes `lehte` from açılış,
    `aleyhte` from kapanış, and `bilinmiyor` from everything else.
- **Catalyst chip.** A board-side reader lists catalysts between now and the
  expiry close:
  - earnings via `report_time`, marked `tahmini` when the source is an
    estimation;
  - FDA events with a precise date, requested with `target_date_min`. Q, H and
    MID targets render `zamanı belirsiz`;
  - macro events matched **by name** from `catalyst.macro_event_names`. Beyond
    `catalyst.macro_horizon_days`, the macro part reads `bilinmiyor`.
- **Scoring provider untouched.** The Phase 3.9 catalyst provider that feeds
  M22/M24 is **not** changed. The probe found:
  - FOMC rows arrive typed `report`, so its `type == "fomc"` filter matches
    nothing (`catalyst_calendar.py:317`);
  - its FDA query has no date filter and is truncated oldest first
    (`catalyst_calendar.py:197`).
  Fixing those changes scoring, so it is a registry item for Berkay. The audit
  block notes that the chip and M22's event score can disagree. The provider's
  `catalysts_in_window` already returns the in-window list the owner asked
  for.

### B5. Regime band

One sentence plus chips, from `alfa_regime`:

- **Market tide.** The last **complete** 5-minute bucket (cumulative since the
  open). Net = net_call_premium − net_put_premium, plus net_volume, with a
  profile dead-band.
- **SPY and QQQ dealer gamma.**
  - Sign: the latest regular-session `gamma_per_one_percent_move_oi`, with a
    profile dead-band.
  - Magnitude: shown as a one-year percentile from `greek-exposure`, with the
    base rate `son 1 yılın {k}/{n} gününde kısa gamma` (probe: SPY 171 of 251
    days).
- **Gamma flip.** `gex-levels source=oi`, labelled
  `en yakın strike işaret değişimi`, with % distance from spot. It is context
  only, never the regime word. The board computes no flip of its own.
- **Vol curve.** SPY ATM IV at the DTEs nearest the profile anchors (30 and
  90), excluding dte 0 and 1–3 DTE event humps. Labelled `SPY IV vadesi`:
  contango, flat or inverted (dead-band in vol points).
- **VIX.**
  - The futures term structure reads
    `VIX vade yapısı: kapsam-dışı (volatilite eklentisi yok)` (probe: 403
    `volatility_scope_required`).
  - VIX spot reads `VIX ≈ {v} (türetilmiş)`, from
    `/api/stock/VIX/volatility/term-structure`.
- **Three tripwires** (`fikrimi ne değiştirir`), each with its current value
  and profile cutoffs:
  1. SPY or QQQ gamma crosses the dead-band.
  2. The tide reverses and holds for `regime.tide_persistence_buckets`
     complete buckets (counted from `alfa_regime` history).
  3. SPY IV30 ≥ IV90 plus the dead-band.
- **Staleness.** Any source older than `regime.max_source_age_seconds` renders
  `bilinmiyor`.
- The band never enters the evidence count.

### B6. Portfolio overlap

- **Prerequisite, verified.** `trade` has structured `ticker`, `direction`,
  `instrument`, `strike`, `expiry` (a free-text string, parsed defensively),
  `contracts`, `entry_price` and `status` (`webapp/journal.py:50-73`). Only
  `thesis` and `exit_reason` are free text, so nothing needed fixing first.
  Unparseable direction, instrument or expiry values render `bilinmiyor`.
- **Badge.** `zaten bu bahittesin` when an open trade has the same ticker and
  direction. The detail shows the matching trade.
- **Capital header.** Sum of open long premium at risk (contracts × entry ×
  multiplier) against `sizing.capital_usd`, in dollars and percent.
- **Single-bet strip.**
  - Strong link: board and open-journal tickers that each weigh ≥
    `portfolio.cluster_min_member_weight_pct` in the same focused ETF
    (`portfolio.focused_etfs`, with holdings ≤ `max_focused_holdings`).
    Clusters are the connected components, sorted alphabetically.
  - Share classes merge through `portfolio.share_class_aliases`.
  - Weak link: the same sector from `alfa_ticker_info`. It is shown but never
    merged into a cluster.
  - Holdings carry their `updated` date.
  - Broad index ETFs are left out of the focused list. Probe: raw exposure
    overlap is dominated by broad funds (NVDA/AAPL Jaccard 0.667).

## 7. FAZ D — delayed evidence families (never counted)

Every item renders under `ek kanıt (gecikmeli)` with:
- the filing (or as-of) date;
- the delay in days;
- the outcome: the underlying's move since the filing date, from
  `alfa_daily_close`.

None of it enters counts, the strength label, the clean-candidate rule or the
counter-argument priority.

**D1 Kongre.**
- The owner-named `/api/congress/unusual-trades` and `/by-tickers` return
  **422 "Missing access … premium endpoint"** on this key (probe 2026-09-15,
  samples `alfa_probe/delayed_congress_unusual*.json`).
- Substitute: `/api/congress/recent-trades?ticker=`.
- Delay = filed_at_date − transaction_date. The row is marked `geç bildirim`
  when the delay is above `delayed.congress_late_days`.
- Side is normalised to alış/satış. Amount stays as the filed USD range.
  `member_type` `executive` is labelled separately.

**D2 İçeriden.**
- `/api/insider/{t}` is only a roster.
- Substitute: `/api/insider/transactions?ticker_symbol=&form_types[]=4&form_types[]=4/A`.
- Only codes P and S count as trades; A, F, G and J are excluded. Scheduled
  `10b5-1` trades are marked.
- `/api/market/insider-buy-sells` is market-wide, filing-dated and partial
  intraday, so it is not used per row.

**D3 Short + FTD.**
- Short interest: `/api/shorts/{t}/interest-float/v2`. The v1 endpoint's data
  ends in 2021 with impossible values. `si_float` is a fraction, displayed as a
  percent. As-of date = `market_date`.
- FTDs: `/api/shorts/{t}/ftds`, dated by the fail date.
- Neither source has a filing date, so the delay is today − as-of date,
  labelled that way.

## 8. ThetaData (K2)

- ThetaData is not wired to the board.
- **Probe, 2026-09-15.** Java 25 is installed, and `creds.txt` and
  `config.toml` exist. The Terminal started and was rejected at login with
  `Invalid credentials`. Port 25503 never opened, so no snapshot request could
  be sent. Log: `alfa_probe/theta_terminal.log`.
- **Documents.**
  - `docs/thetadata-capability-probe.md` records the probe and the re-run
    procedure.
  - `docs/thetadata-decision.md` compares a nightly laptop job pushing to
    Railway Postgres against dropping the subscription until Phase 3.5, and
    ends with one clear recommendation.
- No code.

## 9. Evidence state mapping

Branch → state, relative to option type (§5 A3 swaps `lehte`/`aleyhte` for
sold options). `deg` = a degraded provider call detected for that stage on
that event; it always yields `bilinmiyor`.

| Family | lehte | aleyhte | nötr (not counted) | bilinmiyor | kapsam-dışı |
|---|---|---|---|---|---|
| Akış (net-prem tape) | direction net premium > dead-band | opposite > dead-band | inside dead-band | no or stale tape row | — |
| Dealer gamma (M21, non-directional) | full_short_and_proximate (labelled `hareketi büyütebilir`) | — (never) | partial_one_condition, no_conditions_met, extreme_distance_cutoff | no_data, timeout, deg | — |
| Karanlık havuz (M26) | confirmed_match | direction_mismatch | direction_unclear, no_qualifying_prints | timeout, deg | unknown_option_type |
| Sektör (M25) | strong, moderate | contrarian | weak, all_neutral, empty_peer_flow | timeout, deg; no_sector for a common stock | no_sector when `alfa_ticker_info.issue_type` is ETF or Index (without deg); unknown_option_type |
| Fiyat teyidi (M23) | call_confirmed, put_confirmed | call_contrarian, put_contrarian | neutral | timeout, provider_error, data_missing_neutral | neutral_unknown_type |
| Açık pozisyon (B4 T+1) | açılış | kapanış | — | henüz doğrulanmadı, no confirm row | expires before T+1 |

**Legacy rows** (no telemetry). Only these persisted values map; every other
value is `bilinmiyor`:

| Field | lehte | aleyhte |
|---|---|---|
| M23 `price_confirmation_score` | 1.0 | 0.3 |
| M25 `sector_confirmation_score` | ≥ 0.7 | 0.0 |
| M26 `dark_pool_confirmation` | True | — |

M27's `opening_closing_score` is not print evidence and never maps.

## 10. Tests

**New tests:**
- R-rule render tests, one per §2 id.
- Pure unit tests: aggregation, direction, tradability, evidence mapping (every
  row of the §9 table), narrative, ledger, sizing, moves, chase, OI state,
  regime sentence and tripwires, clusters.
- Writer tests: replay idempotency and the degraded diff.
- A route test on a seeded sqlite run.
- A render-budget test (2,000 signals).
- A zero-UW-calls-per-render test with a recording fake client.
- One **live integration test per new UW endpoint** in
  `tests/integration/test_alfa_live_endpoints.py`. It skips without the key;
  with the key, it asserts real rows and the probed shape.

**Expected D10 changes**, each named in its commit body:
- A7: dashboard route tests asserting removed per-print copy (`Notable flow`,
  `No signals match`) and the score, sort and filter controls.
- `conviction()` tests, if the webapp stops using the helper. If other callers
  remain, the helper stays.
- The vol board and `/gamma` gain R-IV1; their existing assertions stay.

**Gate per commit:**
`env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ webapp/ && uv run ruff check . && uv lock --check`

## 11. Deploy per FAZ

Each FAZ ends with a PR, merged with a merge commit once the CI `gate` check
is green. Railway then deploys, and the deploy is verified:
- `/health` returns 200;
- the auth wall returns 401 without credentials and 200 with them;
- the board renders;
- today's live run keeps ingesting;
- the new tables fill;
- the deployment log shows no errors.

A broken deploy is rolled back with `git revert -m 1` on the merge, or by a
Railway redeploy of the previous deployment.

## 12. Commit plan

| # | Scope |
|---|---|
| 5.2.0 | hotfix: newest iv-rank row (shipped, PR #9) |
| 5.2.A0 | this contract, the decision-cards contract, the decisions log |
| 5.2.A0b | board foundation: `profiles/board_v1.yaml`, `BoardSettings`, `alfa_` DB base, frozen copy, forbidden-word guard |
| 5.2.A0c | stage telemetry and print-meta writer on the live worker |
| 5.2.A1 | per-ticker aggregation on `/alfa` |
| 5.2.A2 | quotes refresher (single client, daily-count soft cap), tradability chip, cost gate |
| 5.2.A3 | four-state evidence, net-premium tape, ticker info |
| 5.2.A4 | evidence strip, evidence word, score moved to the audit block |
| 5.2.A5 | reason sentence and mandatory `AMA` |
| 5.2.A6 | penalty ledger |
| 5.2.A7 | `/` becomes the Alfa Board; per-print cards removed |
| 5.2.B1 … B6 | FAZ B items, each with its data layer |
| 5.2.C1 … C3 | FAZ C, per its contract |
| 5.2.D1 … D3 | FAZ D items |
| 5.2.K2 | ThetaData probe and decision documents |
| 5.2.Z | `docs/alfa-board-ozet.md` and closeout |

An item may split into a data commit and a UI commit (e.g. `B5a` and `B5b`).
Each commit stays green on its own.

## 13. Non-goals

- No change to:
  - scoring or stages;
  - calibration profile thresholds;
  - `StoredSignal`, `SignalRow` or `TradeRow`;
  - the gamma refresh loop.
- No fix to the Phase 3.9 catalyst provider's FOMC and FDA scoring inputs.
  That is a registry item for Berkay.
- No M28 validator run on live rows. B4 uses its own append-only confirmation
  table.
- No ThetaData code.
- No order execution, routing, or sizing for execution.
