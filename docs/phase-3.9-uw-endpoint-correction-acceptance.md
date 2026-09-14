# Phase 3.9 — Unusual Whales endpoint correction (acceptance contract)

Status: **FROZEN** (per D1) once code lands against it.
Owner: Berkay. Author: Claude (Opus 5).

---

## 1. Objective

Every Unusual Whales call on `main` targets a real, live-verified endpoint
and parses the real response, so the Phase 3.8 screener
(`screener --source rest`) produces a filled, confluence-scored list with
M21–M27 actually fed by data.

Before this phase, every guessed path returned HTTP 404 (verified
2026-09-14): `/api/option-flow/recent`,
`/api/stock/{t}/greek-exposure-strike`, `/api/darkpool/{t}/prints`,
`/api/stock/{t}/upcoming-events`, `/api/option-contract/{sym}/iv-rank`,
`/api/option-contract/{sym}/open-interest`. The integration smokes that
would have caught this skip without a key, so it was never observed.

## 2. Sources of truth (in priority order)

1. **Live responses** captured on 2026-09-14 with Berkay's active key.
2. **`origin/phase-3`** (never merged into `main`; diverged at `b7de170`):
   Phase 3.3.9 path migration, 4.6 flow-alerts mapping and 4.18 429
   handling were built against the live API in May–June 2026.
3. **Vendored OpenAPI spec** `docs/vendor/unusualwhales-openapi.json`
   (fetched from `/api/openapi`; the server returns YAML content).

The spec is wrong against live responses in several places. Where they
disagree, live wins:

| Topic | Spec | Live |
|---|---|---|
| `iv-rank.iv_rank_1y` scale | example `"0.65"` | `"40.3312"` (0–100) |
| flow-alerts `newer_than` | "accepts ISO" | ISO ignored; epoch seconds honoured |
| flow-alerts list row | no `id`/`bid`/`ask`/`iv_end` | all present on `/api/option-trades/flow-alerts` |
| `/flow-recent` | "flow per expiry" object | top-level array of the last 50 trades |
| `/option-contract/{id}/historic` | no `date` param | `date` param ignored; all days returned |

`origin/phase-3` is also wrong in places. These are not ported:
earnings `report_time` vocabulary (`after-hours` never occurs live), OI
row selection (`chains[0]` is always today's row), `flow-recent` for
peer flow (8 seconds of tape), and `greek-exposure/strike` treated as
USD/1%.

Berkay chose on 2026-09-14 to port the live-verified layer into `main`
as this phase, rather than follow the spec alone.

## 3. Endpoint decisions

`ET` means `America/New_York`. An "ET date of X" is `X.astimezone(ET).date()`.

### 3.1 REST flow source (`rest_flow.py`, shared mapper)

- `GET /api/option-trades/flow-alerts`, params `ticker_symbol=<comma
  tickers>`, `limit=200` (API max), `older_than=<epoch s>`, plus
  `newer_than=<epoch s>` when a lookback is set.
- **Pagination** (new shared helper): repeat with `older_than` = the
  oldest `created_at` on the page. Dedupe by `id`. Stop when the page is
  short, holds no new ids, or its oldest row is before the cutoff.
- The cutoff is `before - lookback` when `--since-minutes` is given.
  Otherwise it is 00:00 ET of the ET date of the newest alert, i.e. the
  latest session. This supersedes the Phase 3.7 "single response" default,
  which silently covered ~3 hours of one session.
- Mapper aliases: the existing WS names keep priority and REST
  flow-alert names are added. WS behaviour is unchanged.

  | RawPrint | WS name | flow-alert name |
  |---|---|---|
  | timestamp | `executed_at` | `created_at` (publication time; no look-ahead) |
  | option_type | `option_type` | `type` (only `call`/`put`) |
  | premium_paid | `premium` | `total_premium` |
  | spot_price | `spot_price` | `underlying_price` |
  | implied_volatility | `implied_volatility` | `iv_end` (numeric string) |
  | is_iso | `is_iso` | `has_sweep` |
  | tags | `alert_type` | `alert_rule` |

- **fill_side** (new decision, superseding Phase 3.7 §4 for flow-alert
  rows only): when a row has no `side`/`side_classification` label but
  carries `total_ask_side_prem`/`total_bid_side_prem`, then ask > bid →
  `at_ask`, bid > ask → `at_bid`, otherwise `unknown`.
  - 3.7 forbade inventing a fill side from a *directional* label. These
    fields are UW's per-trade aggressor classification aggregated by
    premium, so they are fill data.
  - This is the rule `origin/phase-3` 4.6 ran live.
  - `at_ask` is used, never `above_ask`: the split cannot distinguish the two.
- `bid`, `ask` and IV stay required. A row missing any of them is
  dropped, counted, and has its keys logged; values are never fabricated.
  A print without IV would abort fusion.
- Multi-leg alerts are tagged `uw:multileg` and kept. Excluding them is a
  strategy call.

### 3.2 M21 dealer gamma (`dealer_gamma.py`)

- `net_gamma_dollars` comes from `GET /api/stock/{t}/spot-exposures?date=<ET date of at>`,
  field `gamma_per_one_percent_move_oi`, taken from the latest row with
  `time <= at`. This is the only live field in USD per 1% move, the unit
  of `DealerExposureAggregate` and of the profile's
  `short_gamma_threshold`.
- `flip_strike` comes from `GET /api/stock/{t}/greek-exposure/strike?date=<ET date of at>`
  (`call_gex + put_gex` per strike). The existing `_find_flip_strike` is
  unchanged. A zero crossing is unit-invariant, so share-gamma is safe
  for the flip.
- `net_gamma_at` (per-strike DTO, no pipeline consumer) sums
  `call_gex + put_gex`. `flow_direction` is `"neutral"` because it is not
  published.
- Cache key: `(ticker, ET date)`.

### 3.3 M24 IV rank (`iv_history.py`)

- `GET /api/stock/{t}/iv-rank?date=<ET date of at>`.
- Row selection: take the latest row with `date < ET date(at)`. A row
  dated the same ET day is used only if `updated_at <= at`; same-day rows
  publish after the close.
- `iv_rank_252d = float(iv_rank_1y)` with no scaling (live is 0–100).
  `implied_volatility = float(volatility)`. `as_of = updated_at`.
  Percentile and intraday change are `None`.
- Cache key: `(ticker, ET date)`.

### 3.4 M26 dark pool (`dark_pool.py`)

- `GET /api/darkpool/{t}?date=<ET date>&newer_than=<ISO>&older_than=<ISO>&limit=500&order_by=premium&order=desc`.
- Rows are fetched per hour bucket: `[floor_hour(before) - window, floor_hour(before) + 1h]`,
  cached by `(ticker, bucket, window)`.
- Client-side filter always applies:
  - `before - window <= executed_at <= before`
  - `trf_executed_at <= before` when present
  - canceled prints are skipped
- `side_estimate` follows the frozen 3.3.9 rule:
  - `price >= nbbo_ask` → `above_ask`
  - `price <= nbbo_bid` → `at_or_below_bid`
  - otherwise `midpoint`
  - missing, non-positive or crossed NBBO → `unknown`
- `side_estimate` is `unknown` when a print is not priced against the
  NBBO: `sale_cond_codes` in {`contingent_trade`, `average_price_trade`,
  `prior_reference_price`}, `trade_code` in
  {`qualified_contingent_trade`, `derivative_priced`}, or non-null
  `ext_hour_sold_codes`.

### 3.5 M22 catalyst calendar (`catalyst_calendar.py`)

- **Earnings:** `GET /api/earnings/{t}`. Includes the upcoming report as
  `source=estimation`.
  - `report_time` `premarket` → 09:30 ET of `report_date`.
  - `postmarket` or `unknown` → 16:00 ET of `report_date`.
  - Announcement date is kept (M22's same-day blackout pin holds); the
    title records source and timing.
- **FDA:** `GET /api/market/fda-calendar?ticker=<t>&limit=200`. Only
  precise dates are used: `target_date` if it is `YYYY-MM-DD`, otherwise
  `start_date` when it equals `end_date`. `when` is 16:00 ET of that
  date. Quarter/half/mid/late targets are dropped.
- **FOMC:** `GET /api/market/economic-calendar`, rows with `type == "fomc"`
  only, applied to every ticker (frozen 3.4: "earnings, FDA, Fed").
  Reports and fed speakers are not catalysts.
- Each sub-fetch is isolated: 404/422 on one source yields empty for
  that source only.
- One provider instance is shared by M22 and M24 in `live_stages.py`.
- 09:30 and 16:00 ET are exchange session boundaries, not tunable
  thresholds.

### 3.6 M27/M28 open interest (`open_interest.py`)

- `GET /api/option-contract/{OCC}/historic`, rows under `chains`. One
  fetch per contract is cached and serves both methods.
- `at(when)` returns the latest row with `date <= ET date(when)`.
  `next_day(trade_date)` returns the earliest row with `date > trade_date`.
  `as_of` is 00:00 ET of the row date.
- Live semantics are start-of-day: row D is present before D's open and
  equals OI after D-1's trading. M27 therefore measures the OI built
  during D-1 (prior-close row D-1 vs event-day row D). Intraday opening
  is not observable in daily OI. This is recorded for Berkay and is not a
  stage change.
- Rejected alternatives:
  - `/api/stock/{t}/oi-per-strike` sums OI across all expiries at a
    strike, which destroys contract-level deltas.
  - `/api/stock/{t}/oi-change` returns every contract of the ticker per
    call, too large for M27's 2 s timeout on SPY/NVDA/TSLA.
- 404/422 → `None`.

### 3.7 M25 sector peers (`sector_peer.py`)

- `sector_of`: `GET /api/stock/{t}/info` → `data.sector`; an empty value
  (ETF/index) gives `None`.
- `peers_of`: `GET /api/screener/stocks?sectors[]=<sector>&order=marketcap&order_direction=desc&issue_types[]=Common Stock`,
  tickers in that order excluding self. This implements frozen 3.4's
  "top 5 peer tickers". `/api/stock/{sector}/tickers` is alphabetical
  (1749 names), so its first five are arbitrary.
- `recent_flow`: the §3.1 flow-alerts helper with `ticker_symbol=<peers>`
  over the hour bucket `[floor_hour(before) - window, floor_hour(before) + 1h]`,
  cached by `(peers, bucket, window)`, then filtered to
  `[before - window, before]`.
  - Direction comes from option `type` and the side-premium split:
    call & ask>bid → bullish, call & bid>ask → bearish, put & ask>bid →
    bearish, put & bid>ask → bullish.
  - Multi-leg alerts and ties → neutral.
  - Label is `alert_rule`.

### 3.8 M23 price action (`price_action.py`) — look-ahead fix

- Endpoint unchanged; verified live.
- `spot_at` is the close of the latest bar with `end_time <= at`. Before
  this fix it was the close of a bar that started at or before `at` but
  printed after it.
- `spot_lookback_ago` is the open of the earliest bar with
  `start_time >= window_start`.
- Cache key: `(ticker, ET date)`.

### 3.9 Client (`client.py`)

- HTTP 429 → `UnusualWhalesRateLimitError`, retried with backoff (port
  of phase-3 4.18).
- HTTP 404/422 → `UnusualWhalesNotFoundError` (subclass of
  `UnusualWhalesAuthError`). It does not count toward the circuit
  breaker, because it is an input miss, not service health. Providers map
  it to their documented no-data return.
- 401/403, 5xx after retries, and an open breaker still raise: a bad key
  must never look like "no data".
- Top-level JSON arrays are wrapped as `{"data": [...]}`.

## 4. Non-goals and flagged items (not changed here)

- **No profile edits.** phase-3 4.18 lowered the UW rate to 1.5 req/s;
  `main` stays at 2.0. Live headers show a 30k/day token limit.
- **Stages are unchanged.** Transient provider errors (5xx after
  retries, open breaker) still propagate; only M23 degrades. The
  stage-level `provider_error` branch is a candidate follow-up.
- **WS live source** (`live.py` `UnusualWhalesLiveSource`) targets a URL
  and protocol the spec contradicts. Out of scope.
- **M27 interpretation** under start-of-day OI (§3.6) and the M21
  flip-strike algorithm on full chains: flagged for Berkay.
- **M23 session-open clamp** (`14:30 UTC` is 09:30 EST, not EDT): stage
  and profile issue, flagged.
- **M26 notional** uses `int(price) * size` (truncation): stage issue,
  flagged.
- **Point-in-time membership** (`screener/stocks`, `info.sector`) is
  current-as-of-fetch. It must not be used in historical replay without a
  date-aware source.

## 5. Tests

- Unit tests that pinned guessed paths or shapes are rewritten against
  live-shaped fixtures (trimmed real rows). This is a class-level D10
  flag: the old fixtures encoded endpoints that never existed.
  Behavioural assertions are preserved (protocol conformance, caching,
  window filtering, None/empty on no data, malformed-row skip).
- Hermeticity: the three missing-key tests stop reading the developer's
  real `./.env`.
- Live integration tests (`tests/integration/test_uw_live_endpoints.py`)
  cover every provider and the REST flow source.
  - They assert real data and the live semantics this contract depends on:
    - `iv_rank_1y` ∈ [0, 100]
    - epoch cursors are honoured
    - flow rows carry `id`/`bid`/`ask`/`iv_end`
    - historic rows are start-of-day
    - spot-exposure rows are in USD
  - Live contracts are discovered from flow-alerts; nothing is hard-coded.
  - Tests skip cleanly without `UNUSUAL_WHALES_API_KEY`, and skip (not
    fail) when the market returns no rows.
- Per-commit gate (hermetic, keys unset):
  `env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ && uv run ruff check .`
- Live gate at closeout: `set -a; source .env; set +a;` then
  `uv run pytest -m integration tests/integration/test_uw_live_endpoints.py`
  followed by the §6 screener command.

## 6. Done when

- Every UW call on `main` hits an endpoint that returned 200 live, with
  the parsing in §3.
- `uv run python -m uoa_detector screener --source rest --live-tickers AAPL,MSFT,NVDA,TSLA,SPY --top-n 15`
  prints a non-empty table.
- The stderr diagnostic shows `ok=` counts for the M-modules that have
  data, and `dropped=0` or dropped keys explained.
- Every commit is green on the §5 gate. No `profiles/*.yaml` change, no
  frozen doc edit, no `# TODO`.
- The PR description states endpoints fixed, the screener result, the
  diagnostic line, and the §4 flags.

## 7. Commit plan

| # | Scope |
|---|---|
| 3.9.1 | this contract + vendored spec |
| 3.9.2 | test hermeticity (missing-key tests ignore local `.env`) |
| 3.9.3 | client: 429 retry, 404/422 `NotFound` outside the breaker, array wrap |
| 3.9.4 | flow-alerts paginator + REST flow source + mapper aliases |
| 3.9.5 | M21 dealer gamma (spot-exposures + greek-exposure/strike) |
| 3.9.6 | M24 IV rank |
| 3.9.7 | M26 dark pool |
| 3.9.8 | M22 catalyst calendar (+ shared instance in `live_stages`) |
| 3.9.9 | M27/M28 open interest |
| 3.9.10 | M25 sector peers + peer flow |
| 3.9.11 | M23 price-action look-ahead fix |
| 3.9.12 | live integration tests + docs (`MODULES.md`, `DATA_INTEGRATION.md`) + closeout |
