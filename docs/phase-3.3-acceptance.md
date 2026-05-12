# Phase 3.3 — Real data integration + module implementations

This is the working acceptance doc for Phase 3.3, derived from the
ThetaData Pro + Unusual Whales full-API setup. It supersedes the
earlier "M21-M28 stays stubbed through Phase 3" plan because the
data feeds the real implementations need are now both available.

Phase 3.3 has six sub-phases. They run in dependency order — each
needs the previous one done. Total estimated scope: 25-40 commits,
1500-3000 LoC, 100-200 new tests.

## Approved decisions (locked in before implementation)

- Both ThetaData Pro and Unusual Whales full API are subscribed.
  Credentials live in environment variables, never committed.
- Single source of truth for raw flow: ThetaData OPRA tape.
  UW's pre-classified flow is consumed for cross-validation
  (SourceFusion confidence tiers) but the canonical print stream
  is ThetaData. Rationale: ThetaData is raw, latency-low, and our
  own M34 sweep classifier is the system of record for
  classification — relying on UW's pre-labels for the canonical
  stream would couple our edge definition to UW's vendor logic.
- UW is the source of truth for derived feeds (dealer gamma,
  catalyst calendar, IV history, sector flow, dark pool, OI
  changes). These are what M21-M28 need; UW computes them and we
  consume.
- Tier-2 historical download target: 51 tickers × 24 months.
  Estimated 50-200GB on disk. Stored under
  `data/historical/thetadata/{ticker}/{YYYY-MM}.parquet`. Excluded
  from git via `.gitignore`.
- Module implementations land in Phase 3.4, not 3.3. Phase 3.3
  stops at "data feeds wired". Phase 3.4 implements the seven
  modules (M21-M28 except M23 which uses ThetaData spot data, not
  UW). Rationale: separating data wiring from semantic
  implementation keeps the bisectable history clean — if module
  logic is wrong, you bisect 3.4; if data is wrong, you bisect 3.3.
- First real backtest is Phase 3.5, after Phase 3.4 modules are
  real. This is the moment "edge: yes/no" gets answered. Phase
  3.2's 4-cell combinatorial runner consumes Phase 3.5's data.

## Phase 3.3.1 — Credential management

Both ThetaData and UW need API credentials. Phase 1-2 handled config
via Pydantic Settings; Phase 3.3.1 extends this with a strict
"credentials never committed" guarantee.

Scope:

- `src/uoa_detector/config/credentials.py` — `Credentials` Pydantic
  Settings model with fields:
  - `thetadata_api_key: SecretStr | None` (env: `THETADATA_API_KEY`)
  - `thetadata_username: SecretStr | None` (env: `THETADATA_USERNAME`)
  - `unusual_whales_api_key: SecretStr | None` (env: `UNUSUAL_WHALES_API_KEY`)
  - `.env` file support via `pydantic-settings[dotenv]`
- `.env.example` committed at repo root with all keys present but
  values blank, so contributors know what env variables exist.
- `.env` added to `.gitignore` (it is already, but verify).
- Pre-commit hook stub: `scripts/check_no_secrets.sh` greps staged
  changes for high-entropy strings matching common API key formats
  (UW: `^uw_[a-f0-9]{32}$`, ThetaData: their format documented at
  fetch-time). Runs in CI too.
- All adapters (ThetaData, UW) accept credentials by injection, not
  by reading env directly. Config layer reads env, passes
  SecretStr to adapters. Adapters call `.get_secret_value()` only
  at the HTTP request site, never in logs.
- structlog processor added that redacts any field whose name
  matches `*_key`, `*_token`, `*_secret`, `*_password` — defense in
  depth against accidental key logging.

Done when:

- `Credentials` loads cleanly from `.env`
- Missing optional credentials → clear error message naming exactly
  which env variable is missing, when an adapter that needs it is
  instantiated (lazy, not at startup)
- `pytest tests/unit/test_credentials.py` — 8 tests covering load,
  missing, redaction in logs, secret string never in `repr()`
- Pre-commit hook catches a fake `uw_` key inserted into a staged
  file (verified by a CI test that intentionally stages a bad
  string and asserts the hook fails)

## Phase 3.3.2 — ThetaData adapter

ThetaData's API has two relevant surfaces: REST historical (bulk
downloads) and WebSocket live stream. Phase 3.3.2 implements both.

Scope:

- `src/uoa_detector/sources/thetadata/`:
  - `client.py` — `ThetaDataClient` HTTP/WS client. Auth, rate
    limiting (token bucket, profile-tunable), retries with
    exponential backoff, circuit breaker.
  - `historical.py` — `ThetaDataHistoricalDownloader`. Given
    `(ticker, start_date, end_date, output_dir)`, downloads OPRA
    trades and quotes, normalizes to `RawPrint`, writes monthly
    parquet files matching the schema pinned in 3.2.2.1.
    Idempotent: re-running the same range skips months already on
    disk (verified by file presence + row-count check).
  - `live.py` — `ThetaDataLiveSource` implementing `RawFlowSource`
    Protocol. WebSocket subscription, reconnect with exponential
    backoff, watermark handling unchanged (event-time, inherited
    from Phase 2.3.3 fusion contract).
  - `mapping.py` — OPRA → `RawPrint` field mapping. Pure functions,
    fully unit tested. Edge cases: half-day sessions, post-close
    prints, condition codes.
- Profile section `data_sources.thetadata`:
  - `rate_limit_requests_per_second: float = 10.0`
  - `historical_concurrency: int = 4` (Pro plan supports this)
  - `live_reconnect_max_attempts: int = 5`
  - `live_reconnect_initial_backoff_s: float = 1.0`
  - `live_reconnect_max_backoff_s: float = 60.0`

Done when:

- 12+ unit tests, including `tests/integration/test_thetadata_smoke.py`
  marked `@pytest.mark.integration` (skipped by default, runs only
  when `THETADATA_API_KEY` env var present). Smoke test downloads
  one day of AAPL historical data, asserts row count > 0 and parquet
  schema valid.
- `ThetaDataLiveSource` produces a stream identical to the synthetic
  source's behavior at a controlled timing — verified by a
  fixture-driven mock WebSocket server replaying canned OPRA frames.
- Historical downloader verified with the same 5-ticker × 1-month
  fixture used in Phase 3.2.2.

## Phase 3.3.3 — Unusual Whales adapter (provider implementations)

UW's API offers two orthogonal capabilities: (a) live flow stream
that we use for cross-validation, and (b) derived data endpoints
that M21-M28 will consume in Phase 3.4. Phase 3.3.3 implements both
the source adapter and all six derived providers, but does NOT wire
them into modules yet (that's 3.4).

Scope:

- `src/uoa_detector/sources/unusual_whales/`:
  - `client.py` — `UnusualWhalesClient`. Same patterns as ThetaData
    client: auth, rate limit, retry, circuit breaker.
  - `live.py` — `UnusualWhalesLiveSource` implementing
    `RawFlowSource`. UW's flow events are already labeled; we
    preserve UW's labels in `RawPrint.metadata` but our M34 stage
    runs its own classifier and does not consult UW labels. The
    `confidence_tier` lifts from `SourceFusion` automatically once
    both ThetaData and UW are streaming.
  - `providers/` — six provider implementations, each in its own
    file:
    - `dealer_gamma.py` → implements `DealerPositioningProvider`
      Protocol (defined in Phase 2 stubs at
      `src/uoa_detector/providers/dealer_positioning.py`)
    - `catalyst_calendar.py` → `CatalystCalendarProvider`
    - `iv_history.py` → `IVHistoryProvider`
    - `sector_peer.py` → `SectorMapProvider` and
      `PeerFlowProvider` (UW provides both in one feed)
    - `dark_pool.py` → `DarkPoolPrintProvider`
    - `open_interest.py` → `OpenInterestProvider` (with
      `next_day` method for M28's T+1 confirmation)
- Profile section `data_sources.unusual_whales`:
  - Same shape as ThetaData's section
  - `cache_ttl_seconds` per provider type (calendar 3600, gamma
    300, IV history 600, etc.) to reduce API hits
- All six providers are pure data-shipping; no business logic. The
  semantic interpretation (e.g., "is dealer gamma negative enough
  to warrant a signal") happens in the corresponding M21-M28 stage,
  which is Phase 3.4 work.

Done when:

- 25+ unit tests, plus 6 integration smoke tests (one per provider,
  marked `@pytest.mark.integration`, skipped without
  `UNUSUAL_WHALES_API_KEY`).
- Each provider verified with a fixture file containing canned UW
  responses that the test mock server replays.
- `UnusualWhalesLiveSource` integrated into `SourceFusion`'s
  multi-source replay test from Phase 3.2.2.3 — verified that
  `confidence_tier="unanimous"` appears when both ThetaData and UW
  see the same print.

## Phase 3.3.4 — Tier-2 historical download

Bulk download of 51 tickers × 24 months from ThetaData. This is
mechanical work but takes real wall-clock time and disk.

Scope:

- `scripts/download_tier2.py` — CLI driver. Reads
  `data/universes/tier2_starter.csv`, computes the 24-month range
  ending one trading day before today, calls
  `ThetaDataHistoricalDownloader` for each (ticker, month) tuple
  with concurrency 4 (Pro limit). Resume-on-failure: tracks
  progress in `data/historical/.download_state.json`.
- Disk space pre-check: estimate based on first ticker's first
  month size; if estimated total > available disk, abort with
  message.
- Validation step after download: per-ticker record count, file
  schema match, no gaps in monthly coverage. Generates
  `data/historical/.manifest.json` with summary stats.

Done when:

- Script runs end-to-end on a 5-ticker × 3-month subset (test
  invocation), produces validated parquet files
- Production run on full tier-2 (you trigger this manually after
  agent confirms the script works on subset; agent does NOT run
  the full download because it costs real bandwidth and time)
- Manifest file generated, all 51 × 24 = 1224 files present (or
  documented gaps for tickers with shorter listing history)

## Phase 3.3.5 — Live observer mode (parallel optional path)

Independently of historical work, the live adapters from 3.3.2 and
3.3.3 can power a "live observer" mode where you watch real-time
signals during market hours. This is motivation-relevant (you said
you want something visible) but not strictly required for the
backtest path.

Scope:

- CLI: `python -m uoa_detector run --source live --feeds thetadata,unusual_whales`
- Connects both live adapters, fuses, writes decision records to
  NDJSON (existing Phase 2 infrastructure unchanged).
- Profile section `live.dashboard_refresh_seconds: int = 5` for
  the eventual Phase 4 dashboard refresh; not used in 3.3.5
  itself.

Done when:

- Live mode runs cleanly during market hours
- Decision records appear in stdout (or NDJSON file when
  `--output-file` set)
- Smoke test: 60 seconds of live mode produces at least one event
  per tier-2 ticker (run during market hours, marked
  `@pytest.mark.live_market`)

## Phase 3.3.6 — Documentation

`docs/DATA_INTEGRATION.md` covering:

- How to obtain ThetaData and UW credentials
- How to set up `.env`
- How to run tier-2 historical download
- Cost expectations (rough monthly bandwidth, storage)
- Troubleshooting common errors (rate limits, auth failures,
  reconnect storms)

## Cross-cutting acceptance — applies to every commit in 3.3.x

- `pytest -q` green at every commit
- `mypy --strict` clean across all source files
- `ruff check .` clean
- No credentials in any committed file
- Every adapter has lazy initialization: importing the module does
  NOT trigger network or auth; instantiation does
- `BacktestStoreProtocol`, `RawFlowSource`, and provider Protocols
  from Phase 2 unchanged — Phase 3.3 implements them, doesn't
  redefine them. If a Protocol must change, it is its own commit
  with breaking-change call-out.

## What Phase 3.3 explicitly does NOT do

- Real M21-M28 stage logic (Phase 3.4)
- Walk-forward backtest with real data (Phase 3.5)
- Trade execution, broker integration (out of Phase 3 scope)
- Web dashboard, Telegram alerts (Phase 4)

## Open question awaiting decision before 3.3.4

ThetaData Pro plan limits — actual bandwidth quota and historical
bulk-download terms — need verification at fetch time. If the Pro
plan caps monthly egress below the tier-2 download size, we either
(a) split the download across multiple billing months, (b) negotiate
with ThetaData support, or (c) reduce tier-2 scope. Decision
deferred until Phase 3.3.4 starts and current limits are read from
the ThetaData docs.

## What you (Berkay) need to do before Phase 3.3 starts

1. Confirm ThetaData Pro account is active and `THETADATA_API_KEY`
   + `THETADATA_USERNAME` are at hand.
2. Confirm Unusual Whales API key is at hand. Check at
   `https://unusualwhales.com/account` → API tab.
3. Decide a local directory for `data/historical/` — needs
   50-200GB free. Probably a separate disk if your laptop SSD is
   tight.
4. Add `THETADATA_API_KEY`, `THETADATA_USERNAME`,
   `UNUSUAL_WHALES_API_KEY` to a `.env` file at repo root before
   running any 3.3.x commands. The agent never sees your real
   keys; they live only in your local `.env`.

---

This document is the contract for Phase 3.3. It will not be
revised mid-implementation; if a decision needs revisiting, that
discussion happens between sub-phases, not within them.

---

# Phase 3.3 — COMPLETE

Phase 3.3 closed on `2269470` (Phase 3.3.5.4) plus the docs +
cleanup work in `dc6ff38` / `4651b60` / this commit. All six
sub-phases shipped in their original dependency order with no
acceptance-doc revisions mid-implementation.

## Sub-phase summary

| Sub-phase | HEAD | Tests | Sub-commits | Key delivery |
|---|---|---|---|---|
| 3.3.1 — Credentials | `e92db7c` | 631 | 4 | `Credentials` Pydantic model, `redact_secrets`, `.env.example`, pre-commit hook |
| 3.3.2 — ThetaData | `1e7c0b7` | 769 + 3 skip | 6 | HTTP client + retries + breaker; historical bulk downloader; live WS source; mapping utilities |
| 3.3.3 — Unusual Whales | `a1e473c` | 885 + 11 skip | 6 | HTTP client; live WS source; six derived providers (gamma, calendar, IV history, sector, peer, dark-pool, OI); SourceFusion 'unanimous' tier pin |
| 3.3.4 — Tier-N download | `233c8d6` | 977 + 11 skip | 5 | universe CSV reader; state machine (`.download_state.json`); orchestrator (concurrency + resume); validation + manifest; `scripts/download_tier2.py` CLI |
| 3.3.5 — Live observer | `2269470` | 1019 + 12 skip | 4 | `LiveSettings` profile section; `LiveObserver` graceful shutdown; live source factory; CLI `--source live --feeds ...` + `live_market` smoke |
| 3.3.6 — Closeout | this | 1014+ + 12 skip | 3 | `docs/DATA_INTEGRATION.md`; `legacy_stub.py` deletion; this acceptance summary |

## Phase 3.3 totals

  - 6 sub-phases, 28 sub-commits (all bisectable; per-commit pytest
    sweep confirmed at every closeout)
  - ~1014 tests green + 12 skipped (3 ThetaData smoke + 8 UW smoke
    + 1 live_market smoke; all gated by API keys + market hours
    where applicable)
  - 109 source files, mypy --strict + ruff clean across the surface
  - ~9000 LoC across `src/`, `tests/`, `scripts/`, `docs/`
  - ~180 documented judgment calls in commit messages (search
    `decision (` in `git log` for the catalogue)

## Judgment-call themes (representative selection per sub-phase)

3.3.1 — Credentials
  - SecretStr everywhere; never plain str for secrets
  - Pre-commit hook + CI gate so .env never enters git
  - Redact-secrets log filter applied at the structlog handler

3.3.2 — ThetaData
  - HTTP layer: timeout / retry / circuit-breaker as composable
    middleware in `_http_base.py` (DRY across both vendors)
  - Historical: per-contract per-month parquet idempotency check
    (file presence + row_count > 0)
  - Live: reconnect with exponential backoff up to N attempts;
    `ReconnectExhaustedError` surfaces only after backoff ceiling

3.3.3 — Unusual Whales
  - Six providers share a TTL cache (`_cache.py`) for DRY
  - Providers are pure data-shipping (no semantic logic) — Phase
    3.4 stages own the interpretation
  - SourceFusion confidence_tier='unanimous' pin via
    multi-source synthetic test (no real key needed)

3.3.4 — Tier-N download
  - State file uses Pydantic with extra=forbid + schema_version
    raise on mismatch
  - Per-task atomic checkpoint (one disk write per (ticker, month)
    completion)
  - LAYOUT FIX during 3.3.4.5: contract_output_dir drops ticker
    layer to avoid double-ticker bug in path scheme

3.3.5 — Live observer
  - LiveObserver does NOT own pipeline construction (caller owns
    Pipeline; observer only adds shutdown plumbing)
  - --feeds thetadata gated until Phase 4 in CLI (factory surface
    supports it; only CLI glue deferred)
  - live_market marker registered separately from `integration`
    because timing matters (skip outside market hours)

3.3.6 — Closeout
  - DATA_INTEGRATION.md is operator-facing (imperative voice);
    phase-3.3-acceptance.md stays the design reference
  - Polygon + IBKR stubs flagged for retirement but NOT deleted
    in 3.3.6 (vendor-strategy decision deferred to Berkay
    explicitly)

## What Phase 3.4 unblocks

Phase 3.3 was a prerequisite for the M21–M28 stage rewrites. Phase
3.4 will:

  - Wire each of the six UW providers to its corresponding stage
    (gamma → M22, catalyst → M23, IV history → M24, sector → M25,
    peer → M26, dark-pool → M27, OI → M28)
  - Wire ThetaData live to the multi-feed CLI (per-contract
    subscription enumeration via Phase 3.3.4.5's lister + snapshot
    resolver)
  - Add trading-calendar overlay so manifest gap-detection
    auto-classifies weekends vs real gaps
  - Phase 4 dashboard will start consuming
    `live.dashboard_refresh_seconds`

## Phase 3.4 prep checklist (Berkay)

Before kicking off Phase 3.4, verify locally:

  1. **Credentials in env**
     - `THETADATA_API_KEY`, `THETADATA_USERNAME` set in `.env`
     - `UNUSUAL_WHALES_API_KEY` set in `.env`
     - `cp .env.example .env` then fill in (the .env is gitignored
       and the pre-commit hook scans for accidental commits)

  2. **Theta Terminal running locally**
     - Download from ThetaData dashboard, run `java -jar
       ThetaTerminal.jar`
     - Verify: `curl http://127.0.0.1:25510/v2/list/exchanges`

  3. **Smoke tests during market hours (Mon-Fri 09:30-16:00 ET)**
     ```
     uv run pytest -m integration -v
     # Expects: 11 tests, no skips (3 ThetaData + 8 UW)
     uv run pytest -m live_market -v
     # Expects: 1 test, no skip (live observer)
     ```

  4. **Optional: small subset historical download**
     ```
     uv run python scripts/download_tier2.py \
       --tier tier2_starter \
       --start-date 2024-01-01 --end-date 2024-01-31 \
       --max-tasks 5 --max-contracts 20
     ```
     Verify `.download_state.json` shows 5 done + `.manifest.json`
     summary is reasonable. ~50 MB disk, ~5 minutes.

  5. **Polygon / IBKR stub retirement decision**
     - Read sources/__init__.py docstring (notes both as candidates)
     - Decide: keep as-is, retire to a separate phase, or retire
       in Phase 3.4 prep commit
     - This is the only outstanding cleanup question from
       Phase 3.3

  6. **Phase 4 design alignment**
     - Re-read `docs/UOA_Convexity_Detector_v5.docx` M21–M28
       module specs
     - Confirm the six UW provider data shapes match the stage
       inputs M21–M28 expect (the providers ship raw fields; the
       stages will compute M-specific metrics)

When all six items are confirmed, Phase 3.4 can begin with
confidence that the data layer is stable and validated.

This document is the closing contract for Phase 3.3. Phase 3.4
will get its own working acceptance doc.

---

# Phase 3.3.7 addendum — ThetaData v2 → v3 migration

Phase 3.3.7 is an UNPLANNED sub-phase inserted between Phase 3.5
acceptance and Phase 3.5.1 (credential validation). ThetaData
released API v3 in 2025; v2 endpoints now return ``410 GONE`` and
the v2 binary that Phase 3.3.2 was built against is no longer
publicly distributed. Phase 3.5.1 hit this on Berkay's first
attempt to validate credentials; the only path forward was a
narrow-scope v3 migration of the existing ThetaData adapter.

The contract for the migration lives in
``docs/phase-3.3.7-acceptance.md`` (frozen 2026-05 per the same
discipline as Phase 3.3 / 3.4 / 3.5). It will not be revised; the
addendum here records what shipped.

## Sub-phase summary

| Sub-phase | HEAD | Tests | Sub-commits | Key delivery |
|---|---|---|---|---|
| 3.3.7 — Acceptance | `b3b36f0` | 1391 + 20 | 1 | Frozen contract; 5 sub-phases; scope narrowed to `src/uoa_detector/sources/thetadata/` |
| 3.3.7.1 — Investigation | `9229cbc` | 1391 + 20 | 1 | `docs/thetadata-v3-migration.md` (559 lines, 13 sections); v3 endpoint mappings + 7 judgment calls (J1-J7) |
| 3.3.7.2 — mapping.py | `3467b1a` | 1412 + 20 | 1 | `iso_timestamp_to_utc_datetime`, `format_v3_strike_param`, `format_v3_right`, `trade_row_from_v3_dict`, `quote_row_from_v3_dict`, TradeRow/QuoteRow `extra="ignore"` |
| 3.3.7.3 — REST surface | `d1fee15` | 1412 + 20 | 1 | client `DEFAULT_BASE_URL` 25510→25503, `request_json` returns `Any` + auto-injects `format=json`, historical v3 URLs + decoder, price_action.py full v3 rewrite |
| 3.3.7.4 — live.py | `c650d41` | 1412 + 20 | 1 | WS URL `/v2/ws` → `/v1/events` (port 25520 unchanged); message format untouched |
| 3.3.7.5 — Smoke + closeout | this | 1412 + 20 | 1 | smoke tests migrated to v3 paths + params; Berkay-filled validation template; this addendum |

## Phase 3.3.7 totals

  - 6 sub-phases, 6 sub-commits (all bisectable; per-commit pytest
    sweep confirmed at each closeout)
  - 1391 → 1412 tests passing (+21 net new for v3 mapping coverage)
  - 4 source files modified in
    `src/uoa_detector/sources/thetadata/`:
    `mapping.py`, `client.py`, `historical.py`, `live.py`
  - 1 source file rewritten:
    `src/uoa_detector/sources/thetadata/providers/price_action.py`
    (discovered to be a v2 caller during 3.3.7.3 implementation)
  - Test files updated: `test_thetadata_mapping.py`,
    `test_price_movement.py`, `test_thetadata_live.py`,
    `test_thetadata_client.py`, `test_thetadata_smoke.py`,
    `test_m23_smoke.py`
  - 7 judgment calls (J1-J7) all resolved or scheduled in 3.3.7.1
    spec; 8 additional sub-phase-specific judgment calls in commit
    messages
  - mypy --strict + ruff clean across 111 source files at every
    sub-commit

## What Phase 3.3.7 changed in the contract surface

These items LOOK like behavior changes but are wire-format only;
the canonical types (`OptionsContract`, `RawPrint`, `OptionType`,
`OptionRight`) are unchanged.

  - **Default REST port**: 25510 → 25503
  - **Default WS port**: 25520 unchanged
  - **REST URL paths**: `/v2/hist/option/{trade,quote}` →
    `/v3/option/history/{trade,quote}`; `/v2/list/roots/option` →
    `/v3/option/list/symbols`; `/v2/hist/stock/ohlc` →
    `/v3/stock/history/ohlc`
  - **WS URL path**: `/v2/ws` → `/v1/events`
  - **REST query params**: `root` → `symbol`; `exp` → `expiration`;
    `strike` 1/10-cent int → dollar string (`"170.00"`); `right`
    `'C'`/`'P'` → `'call'`/`'put'`; `ivl` ms-int → `interval` str
    (`"1m"`)
  - **REST response shape**: `{header.format, response: [[...]]}`
    positional → top-level array of named-dict objects with ISO
    timestamps
  - **WS message shape**: UNCHANGED (still positional integer
    timestamps, 1/10-cent strikes, 'C'/'P' rights — Streaming API
    has independent versioning)
  - **`format=json`**: now auto-injected into every REST request
    (v3 default response format is CSV)

## Judgment-call themes

  - **Dual-format mapping module** (J1-J4): REST uses dollar strings
    + 'call'/'put' + ISO timestamps; WS still uses 1/10-cent
    integers + 'C'/'P' + (date, ms_of_day) integer pairs. mapping.py
    exports BOTH parser families so historical (REST) and live (WS)
    callers each have the right boundary.
  - **`extra="ignore"` on TradeRow/QuoteRow** (J2): v3 adds redundant
    fields (symbol/expiration/strike/right per row). Silently
    dropping them is safer than rejecting; forward-compat for future
    v3 field additions.
  - **`request_json` always-injects `format=json`** (J5): promoting
    from per-call to global ensures callers can't accidentally omit.
    Idempotent setdefault preserves caller-supplied value.
  - **Defensive legacy v2-envelope fallback in v3 decoders**: all
    three v3 decoders (trade, quote, OHLC) accept BOTH the v3 array
    shape AND the legacy v2 envelope. Cost: a few isinstance checks.
    Benefit: graceful degradation if Terminal version skew surfaces.
  - **Streaming WS path is `/v1/events` not `/v3/events`**: v3
    streaming endpoint does not exist; streaming API is on its own
    version line (v1) independent from REST.
  - **price_action.py in scope, not separate sub-phase**: Discovered
    as v2 caller during 3.3.7.3 implementation. Acceptance contract
    scope is "src/uoa_detector/sources/thetadata/" — the package,
    not hand-listed files. Atomic v2 → v3 cutover preserved.

## Backward-compat invariants confirmed

  - **Parquet schema** (`parquet_schema.RAWPRINT_PARQUET_SCHEMA`)
    unchanged. Phase 3.5.3 download produces identical files on
    disk.
  - **M23 provider injection seam** (`PriceActionProvider` Protocol +
    `ThetaDataPriceActionProvider` implementor) holds across the v3
    backend swap. Phase 3.4.3 stage code is unchanged.
  - **Phase 3.3.4 download script** (`scripts/download_tier2.py`)
    unchanged; the v3 migration is below the script's call surface.
  - **Auth / rate-limit / retry / circuit-breaker** logic in
    `_http_base.py` (Phase 3.3.2 middleware) unchanged; independent
    of v2 vs v3 endpoint paths.
  - **OPRA condition codes + exchange codes** (`OPRA_DROP_CONDITIONS`,
    `_EXCHANGE_NAMES`) unchanged; these are OPRA Pillar standards,
    not ThetaData-specific.

## Validation status

Phase 3.3.7.5's done-when criteria are recorded in
``docs/phase-3.3.7-validation.md``. As of the Phase 3.3.7.5 commit:

  - Unit-test layer: 1412 passing + 20 skipped, mypy strict + ruff
    clean (no regressions from any sub-commit)
  - Real-Terminal validation: PENDING — Berkay runs the 4-test
    smoke (3 ThetaData + 1 M23) locally against Theta Terminal v3
    + ``THETADATA_API_KEY``, then fills the Results section in
    ``docs/phase-3.3.7-validation.md`` and commits.

## Phase 3.5.1 resume notes

After Phase 3.3.7 closes (Berkay-validated), Phase 3.5.1
(credential validation — paused at Phase 3.3.7 insertion) resumes
where it left off:

  1. Theta Terminal v3 stays running (no re-setup).
  2. ``.env`` already loaded.
  3. ``docs/phase-3.5.1-validation.md`` template waits for the
     remaining 16 integration smokes (8 UW + 8 M21-M28 + 1
     live_market) since the 3 ThetaData + 1 M23 are already
     validated by 3.3.7.5.

Phase 3.5.1 is effectively the UW-side ledger after 3.3.7.5; the
ThetaData side is covered by this addendum.

This addendum closes Phase 3.3.7 from the perspective of code
delivery. Berkay's validation commit (filling
``docs/phase-3.3.7-validation.md``) is the final gate — only then
does Phase 3.5.1 resume.
