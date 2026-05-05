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
