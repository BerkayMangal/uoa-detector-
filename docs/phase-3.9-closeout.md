# Phase 3.9 — Unusual Whales endpoint correction: closeout

Contract: `docs/phase-3.9-uw-endpoint-correction-acceptance.md` (frozen; not
edited by this closeout).

Status: **KAPALI** — every UW call on `main` is live-verified; the screener produces a filled, enriched list; §7 decisions are open for Berkay.

---

## 1. What was wrong

Every UW REST path on `main` was a guess. All of them returned HTTP 404
live (probed 2026-09-14). The integration smokes that should have caught
this skip without a key, so nothing was ever run against the API. The
Phase 3.8 screener ran M21–M27 against 404s.

A second lineage, `origin/phase-3` (never merged; diverged at `b7de170`),
already held a live-verified UW layer: 3.3.9, 4.6, 4.18. Berkay chose on
2026-09-14 to port it. Live probes with his active key then showed that
both the vendored spec and parts of phase-3 are wrong. Contract §2 has
the full list.

## 2. Endpoints (old → new, all HTTP 200 live)

| Module | Before (404) | After |
|---|---|---|
| REST flow (screener) | `/api/option-flow/recent` | `/api/option-trades/flow-alerts` (epoch cursors, paginated over the latest session) |
| M21 net gamma | `/api/stock/{t}/greek-exposure-strike` | `/api/stock/{t}/spot-exposures` (USD/1%, as-of row) |
| M21 flip strike | same | `/api/stock/{t}/greek-exposure/strike` |
| M22 catalysts | `/api/stock/{t}/upcoming-events` | `/api/earnings/{t}` + `/api/market/fda-calendar?ticker=` + `/api/market/economic-calendar` (fomc) |
| M24 IV rank | `/api/option-contract/{sym}/iv-rank` | `/api/stock/{t}/iv-rank` (as-of, 0–100) |
| M25 sector | `/api/stock/{t}/info` (non-existent `peers`) | `/info` → sector; `/api/screener/stocks` by marketcap → peers |
| M25 peer flow | `/api/option-flow/recent` | `/api/option-trades/flow-alerts?ticker_symbol=<peers>` |
| M26 dark pool | `/api/darkpool/{t}/prints` | `/api/darkpool/{t}` (hour bucket, NBBO side, look-ahead guards) |
| M27/M28 OI | `/api/option-contract/{sym}/open-interest` (+ `/eod`) | `/api/option-contract/{sym}/historic` (as-of row, start-of-day) |
| M23 price | `/api/stock/{t}/ohlc/1m` (already correct) | unchanged path; look-ahead fix |

## 3. Commits

| # | Commit | Hermetic gate (pytest) |
|---|---|---|
| 3.9.1 | contract + vendored spec | 1528 passed + 3 env-dependent failures (pre-existing) |
| 3.9.2 | missing-key tests ignore the real `.env` | 1531 passed |
| 3.9.3 | client: 429 retry, daily-limit fail-fast, 404/422 `NotFound` outside breaker, array wrap | 1559 |
| 3.9.4 | flow-alerts paginator + REST flow source + mapper aliases | 1603 |
| 3.9.5 | M21 dealer gamma | 1634 |
| 3.9.6 | M24 IV rank | 1667 |
| 3.9.7 | M26 dark pool | 1730 |
| 3.9.8 | M22 catalysts + shared provider in `live_stages` | 1835 |
| 3.9.9 | M27/M28 open interest | 1853 |
| 3.9.10 | M25 sector peers + peer flow | 1901 |
| 3.9.11 | M23 price-action look-ahead fix | 1920 |
| 3.9.12 | live integration tests + docs + this closeout | 1920 passed, 30 skipped (the 10 new live tests skip without a key) |

- **Gate:** `env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ && uv run ruff check .`
- Every commit from 3.9.2 on is green on that gate. mypy --strict is
  clean (117 source files) and ruff is clean.
- Each unit was implemented, then independently verified, including a
  live smoke against the real API, before it was cherry-picked.

## 4. Live verification (key from `.env`)

- `tests/integration/test_uw_live_endpoints.py`: **10/10 passed**. It asserts:
  - flow rows carry `id`/`bid`/`ask`/`iv_end`
  - epoch cursors are honoured
  - REST flow prints carry spot + IV
  - M21 net is USD/1% (≫ share gamma)
  - IV rank is in [0, 100] with no future row
  - dark-pool prints stay inside the window
  - AAPL earnings are present
  - OI is as-of
  - peers are ranked by marketcap (MSFT/NVDA in AAPL's top 5)
  - the price window uses completed bars
- Existing UW smokes plus the M21–M28 smokes: **16/16 passed** (they
  failed 17 of 17 before this phase).
- Screener ran 2026-09-14 12:52–12:56 UTC with
`uv run python -m uoa_detector screener --source rest --live-tickers AAPL,MSFT,NVDA,TSLA,SPY --top-n 15`.
The run was on Monday before the open, so the latest session was Friday
2026-09-11.

- **Table:** non-empty, 15 candidates, all `SWEEP_UOA`:
  - MSFT C510 2026-11-20 (70DTE), score 0.475
  - AAPL C350 2026-09-18 (7DTE), score 0.430
  - 13 SPY contracts at 0–11DTE, score 0.415–0.425
  - top reasons: `uoa +0.20`, `price_confirmation +0.10` / `gamma +0.06`
- **stderr diagnostic:**
  ```
  flow rows fetched=680, mapped=680, dropped=0
  enrichment: M21 ok=680/nodata=0/err=0 | M22 ok=680/nodata=0/err=0 | M23 ok=673/nodata=7/err=0 | M24 ok=680/nodata=0/err=0 | M25 ok=378/nodata=302/err=0 | M26 ok=570/nodata=110/err=0 | M27 ok=680/nodata=0/err=0
  ```
- **Volume:** 680 signals (SPY 292, AAPL 129, NVDA 126, TSLA 120,
  MSFT 13); 479 UW requests, 0 non-200; 4 min wall time.
  - `/option-contract/{occ}/historic`: 353 requests
  - flow-alerts: 35
  - dark pool: 39
  - other providers: 5 each
- **Labels over all 680:** SWEEP_UOA 168, LEAP_POSITIONING 130,
  IGNORE_NOISE 99, OPENING_UNCONFIRMED 79, CONVEXITY_CLUSTER 60,
  PENALIZED_BELOW_THRESHOLD 56, CONVEXITY_BURST 52,
  OPTIONS_EQUITY_TAPE_CONFIRMATION 36.
- **How to read `ok`:**
  - `ok` means the stage took a scoring branch on provider data; it says
    nothing about quality. M21 `ok` includes `extreme_distance_cutoff`
    (flag 1) and M22 `ok` includes `no_catalyst` (flag 2).
  - M25 `nodata=302` fits SPY having no sector (292 SPY signals) plus a
    few empty peer windows. This is an inference: branch-level counts are
    in the decision records, not in stderr.
  - A pre-market run lists the previous session's flow, including
    contracts that expired on that Friday.
  - Nothing here claims edge; Phase 3.5 owns that verdict.

## 5. Judgment calls

1. **Port order of precedence.** Live responses first, then phase-3, then
   the spec (contract §2). Phase-3 code is not ported where live contradicts it:
   - OI `chains[0]` returned today's row for every date
   - earnings vocabulary `after-hours` never occurs live
   - `flow-recent` for peer flow is ~8 s of tape
   - greek-exposure/strike is share gamma, not USD/1%
2. **Flow source is the market-wide `/api/option-trades/flow-alerts`,** not
   the per-ticker endpoint phase-3 used. The per-ticker rows have no
   `id`/`bid`/`ask`; the market rows have all of them, so nothing is
   fabricated.
3. **fill_side comes from the per-trade aggressor premium split**
   (`total_ask_side_prem` vs `total_bid_side_prem`). This supersedes 3.7 §4
   for flow-alert rows only, as recorded in the 3.9 contract. Consequence:
   live REST prints now reach labeler paths (STANDARD_UOA, HCS gate,
   OPENING_UNCONFIRMED) that ThetaData replay on `main` never reaches.
4. **Prints without open interest are dropped like IV-less prints.** Fusion
   raises on either, so one would abort the run. This goes beyond contract
   §3.1, which names only bid/ask/IV. In the live full-session run 0 of
   680 rows were dropped.
5. **REST prints are yielded in ascending time.** Pages arrive newest
   first, and M38 clustering and decay must not see the future (D9).
6. **Client:** 404/422 raise `UnusualWhalesNotFoundError`, which does not
   count toward the breaker. Providers map it to no-data. 401/403, 5xx
   and an open breaker still raise, so a bad key stays loud.
   `record_success` moved after body parsing, so an HTML 200 cannot reset
   the failure count.
7. **Transport and exchange constants are named module constants,** not
   profile values (D8 applies to scoring): API page limits (200/500),
   09:30/16:00 ET, candle durations.
8. **No profile edits.** phase-3's UW rate change from 2.0 to 1.5 req/s
   was not ported. Live headers show a 30k/day token limit and no binding
   per-minute limit today.

## 6. D10: existing tests changed

Tests that pinned guessed endpoints or row shapes were rewritten against
trimmed live rows, and every change is listed in the corresponding commit
body.

- Behavioural assertions are preserved: protocol conformance, caching,
  window filtering, None/empty on no data, malformed-row skip.
- 3.9.2 changes only env isolation, in 3 tests.
- New behaviour lives in new `tests/unit/test_uw_p39_*.py` files and
  `tests/integration/test_uw_live_endpoints.py`.

## 7. Flags for Berkay (decisions, not bugs in this phase)

1. **M21 flip strike is not usable on full chains.** The frozen
   first-crossing `_find_flip_strike` lands on deep-OTM strikes
   (AAPL 2026-09-11 → 10 with spot ≈ 332; SPY 2026-09-10 → None). M21
   mostly scores `extreme_distance_cutoff` = 0.0, or loses its proximity
   leg. The fix is a stage algorithm change: the crossing nearest spot, or
   UW `gex-levels`.
2. **FOMC catalysts are currently zero.** The live economic calendar types
   the 2026-09-16 rate decision as `type=report`
   ("U.S. interest rate decision"), so contract §3.5's `type == "fomc"`
   rule matches nothing. Changing the rule is a contract decision.
3. **M26 on SPY mostly lands in `direction_unclear`.** 8 of 9 prints ≥ $5M
   in a sampled hour were contingent/QCT, so their side is `unknown` by
   design.
4. **M27 measures the OI built during D-1.** Daily OI is start-of-day, so
   opening flow on the event day cannot be observed. Separately, the
   stage's prior-close timestamp uses the UTC date, which collapses prior
   and current for events after ~20:00 ET.
5. **M23 session clamp** (14:30 UTC = 09:30 EST, not EDT) and
   **no regular-hours bar filter**: stage/profile issues, unchanged.
6. **Transient UW errors abort a run.** A 5xx after retries or an open
   breaker is caught by no stage except M23. The candidate follow-up is a
   stage-level `provider_error` branch.
7. **Point-in-time.** Sector membership, earnings estimations, FDA edits
   and the econ calendar reflect current knowledge. They are not
   replay-safe for historical backtests. The IV token has no history
   before 2026-05-04 (HTTP 403).
8. **M25 peer semantics.** Share classes of one issuer count as separate
   peers: `peers_of('GOOGL')` starts with GOOG, and META's top 5 hold both
   GOOGL and GOOG. Distinct alert rules on the same contract at the same
   instant become separate peer events, sometimes with opposite
   directions. Both follow contract §3.7 literally. Collapsing by issuer
   or by contract+time is a later decision.
9. **WS live source** (`screener --source live`) still targets an
   unvalidated URL. Use `--source rest`.
10. **Out-of-scope `main` bugs found in phase-3 history** (each needs its
   own sub-phase):
   - (a) `orchestrator.py` `store or BacktestStore()` treats an empty store
     as falsy; fixed on phase-3 in `22178e3`.
   - (b) the SimplePnL look-ahead guard for sub-floor-DTE signals
     (phase-3 `b5fcd7a`) is not on `main`'s 3.5.0 engine. This matters
     before any Phase 3.5 verdict.
11. **D11 is not "zero TODO".** Pre-existing TODO markers sit in
    `calibration/resolver.py:48` and `pipeline/stages/__init__.py:4`.

## Sıradaki

Berkay decides the flags in §7. Items 1, 2 and 9(b) matter most before
anyone reads screener scores or a Phase 3.5 result.
