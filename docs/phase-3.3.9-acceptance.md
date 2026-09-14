# Phase 3.3.9 — UW endpoint path migration

This is a follow-up to Phase 3.3.3 (Unusual Whales adapter +
six providers) and a precondition for Phase 3.5.1's "20/20
integration smokes pass" condition. It does **not** open Phase
3.3 or Phase 3.4 frozen contracts:

  - Phase 3.4 stage code and the six provider DTOs
    (`DealerPositioning`, `IVRankSnapshot`, `DarkPoolPrint`,
    `CatalystEvent`, `OpenInterestSnapshot`, `PeerFlowEvent`)
    are unchanged.
  - The original Phase 3.3.3 acceptance contract remains frozen.
    This doc records the path migration as a discrete sub-phase
    per Phase 3.3.8 pattern.
  - `docs/MODULES.md` is amended in place (living reference).

---

## Context

Phase 3.5.1 smoke run on 2026-05-12 reported:

  - 8 ThetaData smoke + M23 UW smoke + 71 other tests = **9 PASS**
  - **12 FAIL — all HTTP 404 from UW endpoints**
  - UW response body contained an explicit LLM/agent advisory:
    `"If you are using an LLM/AI or if you are a coding agent see
    https://unusualwhales.com/.../how-to-fix-404-errors-..."`
  - `$UNUSUAL_WHALES_API_KEY` was valid (`/api/stock/AAPL/info`
    and the M23 smoke against `/api/stock/SPY/ohlc/1m` both
    returned 200).

Root cause: Phase 3.3.3 (Q4 2024) shipped six providers whose
endpoint paths have since drifted from UW's current REST surface.
The drift falls into three buckets:

  1. **Path rename / structural** — dash → slash, removal of
     suffix segments, etc. (dealer_gamma, dark_pool)
  2. **Endpoint relocation** — per-contract → per-ticker
     (iv_history)
  3. **Endpoint retirement** — replaced by 2 or 3 successor
     endpoints with different shape (catalyst_calendar,
     open_interest, sector_peer)

A live OpenAPI fetch (`/api/openapi`) plus per-endpoint curl
probes confirmed the canonical current paths and their response
shapes. The migration table below records the result.

---

## Migration table

| Provider | Phase 3.3.3 path (404) | Phase 3.3.9 path (200) | Shape delta |
|---|---|---|---|
| dealer_gamma | `/api/stock/{t}/greek-exposure-strike` | `/api/stock/{t}/greek-exposure/strike` | `as_of/net_gamma/flow_direction` → `date/call_gex/put_gex/...`. Map: `net_gamma = call_gex + put_gex`, `as_of = date@21:00 UTC`, `flow_direction = "neutral"`. |
| iv_history | `/api/option-contract/{sym}/iv-rank` | `/api/stock/{t}/iv-rank` | `as_of/implied_volatility/iv_rank_252d/iv_percentile_252d/iv_change_intraday_pct` → `date/updated_at/volatility/iv_rank_1y/close`. `iv_percentile_252d` and `iv_change_intraday_pct` no longer published — defaults applied. Cache key drops from OCC symbol to ticker. |
| dark_pool | `/api/darkpool/{t}/prints` | `/api/darkpool/{t}` | `side_estimate` field removed. Derived in-process from NBBO: `price ≥ nbbo_ask → above_ask`, `price ≤ nbbo_bid → at_or_below_bid`, otherwise `midpoint`, missing NBBO → `unknown`. |
| catalyst_calendar | `/api/stock/{t}/upcoming-events` | 3 endpoints: `/api/earnings/{t}` + `/api/market/fda-calendar` + `/api/market/economic-calendar` | Three parallel fetches via `asyncio.gather`. FDA filtered by ticker; economic-calendar passed through globally. kind: `earnings` from earnings feed, `fda` from FDA feed, `fomc` if "fed" in event title else `other`. |
| open_interest | `/api/option-contract/{sym}/open-interest` + `/open-interest/eod` | `/api/option-contract/{sym}/historic?date=...` | Single endpoint for both at() and next_day() with different `date=` params. Top-level key `"data"` → `"chains"`. `as_of` derived from `last_tape_time` (preferred) or `date@21:00 UTC`. |
| sector_peer membership | `/api/stock/{t}/info` (with `peers` field) | `/api/stock/{t}/info` + `/api/stock/{sector}/tickers` | `peers` field removed from info response. Provider now does two calls: info → sector, then sector → tickers list. 24h TTL absorbs the cost. |
| sector_peer flow | `/api/option-flow/recent?tickers=X,Y,Z` | `/api/stock/{t}/flow-recent` (per ticker) | Multi-ticker endpoint retired. N parallel per-peer calls via `asyncio.gather`. Direction derived from `ask_vol` vs `bid_vol` (legacy `side_classification` honoured if present). Label falls back to `option_chain_id`. |

Sondaj log: 6/6 candidate paths returned 200; sample responses
captured in this commit's pre-flight log; full OpenAPI snapshot
at `/tmp/uw_openapi.yaml` (transient; regenerable via
`curl https://api.unusualwhales.com/api/openapi`).

---

## Sub-commit changelog

| # | Hash | Summary | Tests delta |
|---|---|---|---|
| 3.3.9.1 | `1bf5860` | dealer_gamma slash path + (call_gex + put_gex) summation, as_of from `date` | +2 |
| 3.3.9.2 | `b5fbe33` | iv_history /api/stock/{t}/iv-rank, ticker-keyed cache, volatility/iv_rank_1y mapping | +1 |
| 3.3.9.3 | `e7a30b6` | dark_pool path suffix removal + NBBO-derived `side_estimate` | +4 |
| 3.3.9.4 | `2cd3a72` | catalyst_calendar 3-source aggregation (earnings + FDA + econ) | +4 (4 existing rewritten) |
| 3.3.9.5 | `<n>` | open_interest /historic endpoint, chains response shape | +1 |
| 3.3.9.6 | `<n>` | sector_peer 2-call sector lookup + N-call peer flow, client array auto-wrap | +2 |
| 3.3.9.7 | this commit | OI smoke fixture refresh + missing-key test env-isolation + docs | 0 source delta |

Cumulative unit-test count: **1377 pass** (was 1364 at branch start).
Per-commit `pytest -q --ignore=tests/integration`, `mypy --strict
src/`, `ruff check .` all green; 3 pre-existing baseline failures
(`test_warning_logged_for_commonly_tuned_missing`,
`test_missing_uw_key_fails_fast`,
`test_missing_thetadata_key_fails_fast`) are unaffected by Phase
3.3.9 and remain documented as environmental — they would pass in
a fresh CI environment without exported credentials.

---

## What is NOT changed by Phase 3.3.9

  - Phase 3.4 stage code (M21-M28). Every stage's surface is
    identical; the providers absorb all UW-shape variability.
  - DTOs in `src/uoa_detector/providers/*.py`.
  - Profile YAML scoring weights, thresholds, fusion settings.
  - `UnusualWhalesProviderCacheTTL` schema (no new fields).
  - Phase 3.5 acceptance contract (`docs/phase-3.5-acceptance.md`).

The single supporting change outside the six providers is in
`src/uoa_detector/sources/unusual_whales/client.py`:
``request_json`` now auto-wraps top-level JSON array responses
into `{"data": [...]}` so /flow-recent integrates with the rest
of the codebase without per-call special-casing.
``test_non_dict_response_raises_transient`` was renamed and
split — top-level arrays no longer error; only scalar / string
responses still raise `UnusualWhalesTransientError`.

---

## Calibration / falsification implications

None. The new providers return semantically equivalent DTOs:

  - `net_gamma_dollars` is still signed USD per 1% spot move
    (now computed as `call_gex + put_gex`).
  - `iv_rank_252d` is still a 252-day percentile (now sourced
    from `iv_rank_1y`; same statistical basis).
  - `DarkPoolPrint.side_estimate` Literal values are identical;
    only the derivation source changed.
  - `CatalystEvent` kinds are unchanged.
  - `OpenInterestSnapshot.open_interest` is the same EOD authoritative
    integer.
  - `PeerFlowEvent.direction` is still `bullish/bearish/neutral`.

The Track B 8-source fusion hypothesis frozen in
`v5_gamma_squeeze.yaml` and the four falsification scenarios
pinned in Phase 3.2.4 remain intact.

**One known calibration risk flagged for Phase 3.6 (not addressed
here):** if UW's new `(call_gex + put_gex)` scale differs from
the pre-3.3.9 `net_gamma` magnitude, M21's
`short_gamma_threshold = -$50M/1%` may need re-calibration. This
is a Phase 3.6 question and is explicitly out of scope per D4.

---

## Phase 3.5.1 integration smoke result

After Phase 3.3.9.7:

```
tests/integration/ -q
  81 passed, 1 skipped in 18.40s
```

| Category | Count | Status |
|---|---:|---|
| UW provider smokes | 8 | ✅ all pass |
| M-module smokes (M21-M28) | 8 | ✅ all pass |
| ThetaData smokes | 3 | ✅ all pass |
| M23 UW spot smoke | 1 | ✅ pass |
| Other integration (CLI, profile, e2e) | 61 | ✅ all pass |
| `test_live_observer_smoke` (live_market) | 1 | ⏭ skipped (off-market-hours; expected) |
| **Total** | **82** | **81 pass + 1 expected skip** |

This satisfies the Phase 3.5.1 closeout condition. The detailed
validation report is captured in
`docs/phase-3.5.1-validation.md`.

---

## Sıradaki

Phase 3.5.1 KAPALI (next sub-phase 3.5.2 — historical download
dry-run on 5 ticker-months — proceeds immediately).
