# Data Integration Guide

Operator-facing setup for the live + historical data sources the
detector consumes. Phase 3.3 (3.3.1–3.3.5) shipped:

  - Credential plumbing (`SecretStr`, env-var loading, log redaction)
  - ThetaData adapter (HTTP client, historical bulk downloader, live
    WebSocket source)
  - Unusual Whales adapter (HTTP client, live WebSocket source, six
    derived-data providers: dealer gamma, catalyst calendar, IV
    history, sector map, peer flow, dark pool, open interest)
  - Tier-N historical bulk download CLI
    (`scripts/download_tier2.py`)
  - Live observer mode (`uoa-detector run --source live`)

This document tells an operator how to acquire credentials, set up
the environment, run the historical bulk download, and exercise the
live observer. It also lists known troubleshooting paths for the
errors operators will hit in practice.

This doc is **operator-facing**, not an architecture reference. For
the why-and-how of the adapter design see
`docs/phase-3.3-acceptance.md` and the per-module docstrings.

---

## 1. Credential acquisition

### 1.1 ThetaData Pro

ThetaData publishes consolidated OPRA options data (trades, quotes,
end-of-day OI, IV history). The detector uses ThetaData as the
canonical raw flow source — this means the Pro plan is required.

**Plan tier**: ThetaData "Options Pro" (~$80/month at the time of
writing; ThetaData's pricing page is the source of truth).

**Acquisition**:
1. Sign up at https://www.thetadata.net
2. Subscribe to the Options Pro plan (or higher; Standard is not
   enough — it lacks the live OPRA consolidated tape needed by
   `ThetaDataLiveSource`).
3. Receive two values from the dashboard:
   - **Username** (e.g. `your_email@example.com`)
   - **API key** (random hex, 32+ chars)
4. Install Theta Terminal locally — ThetaData's data flows through
   a local proxy that the detector connects to via HTTP/WS:
   - macOS / Linux: download the `.jar` from the dashboard, run
     `java -jar ThetaTerminal.jar` with credentials in env or CLI.
   - The Terminal listens on `127.0.0.1:25510` (HTTP) and
     `127.0.0.1:25520` (WebSocket) by default.
5. Verify the Terminal is up:
   ```
   curl http://127.0.0.1:25510/v2/list/exchanges
   ```
   (Should return JSON listing exchanges.)

The detector connects to the local Terminal, not directly to
ThetaData's cloud — this is by design. The Terminal handles
ThetaData's auth + caching, and the detector uses its own client
for retries / rate limits / circuit breaking.

### 1.2 Unusual Whales

Unusual Whales publishes pre-classified options flow alerts plus
derived data feeds (GEX, IV rank, dark pool prints, etc.). The
detector uses UW for cross-validation of the raw stream and as the
sole source for the derived data Module 21–28 will consume.

**Plan tier**: Unusual Whales "API-Plus" (~$50/month at the time of
writing; UW's API page is the source of truth). The base UW
subscription does not include API access; the API-Plus add-on
unlocks both REST endpoints and WebSocket flow alerts.

**Acquisition**:
1. Sign up at https://unusualwhales.com
2. Subscribe to API-Plus.
3. Locate the API key:
   - Web UI → "Account" → "API" → copy the bearer token
   - The token is a long opaque string (not a username/password
     pair like ThetaData)
4. Verify the key works:
   ```
   curl -H "Authorization: Bearer YOUR_KEY" \
        https://api.unusualwhales.com/api/stock/AAPL/info
   ```
   (Should return `{"data": {...}}` with `sector`, `next_earnings_date`,
   `marketcap`, …)

UW connects directly over HTTPS / WSS — no local proxy. The detector
sends the bearer token in the `Authorization` header on every
request and on the WebSocket handshake.

Every UW REST endpoint the detector calls, with its live-verified
response shape, is listed in
`docs/phase-3.9-uw-endpoint-correction-acceptance.md` §3. The vendored
OpenAPI spec (`docs/vendor/unusualwhales-openapi.json`, YAML content
served by `/api/openapi`) is advisory only: it is wrong against live
responses in several places (§2 of that contract). Live response headers
on 2026-09-14 reported `x-uw-token-req-limit: 30000` requests per day.

---

## 2. Environment setup

### 2.1 Copy the template

```bash
cp .env.example .env
```

### 2.2 Fill in credentials

Edit `.env` (which is git-ignored — never commit it):

```
THETADATA_API_KEY=your_thetadata_hex_key_here
THETADATA_USERNAME=your_thetadata_email@example.com
UNUSUAL_WHALES_API_KEY=your_unusual_whales_bearer_token_here
```

The detector reads these via `Credentials` (a Pydantic Settings
model, Phase 3.3.1.1). On startup, missing keys are reported as
errors that name the env var; a process needing only one feed can
leave the other blank.

### 2.3 Verify with the redact-secrets pre-commit hook

The pre-commit hook in `scripts/check_no_secrets.sh` scans staged
diffs for likely credential strings (UW bearer pattern, ThetaData
hex pattern). Install it once:

```bash
cp scripts/check_no_secrets.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

If you accidentally `git add .env` the hook stops the commit and
prints the offending file + line. The same script runs in CI before
pytest.

### 2.4 Verify Python toolchain

```bash
uv sync                          # install all deps (Python 3.12+, Pydantic v2, etc.)
uv run pytest -q                 # ~1019 tests pass + ~12 skip without keys
uv run mypy --strict src/        # clean
uv run ruff check .              # clean
```

---

## 3. Tier-N historical bulk download

Phase 3.3.4 ships `scripts/download_tier2.py` for chained per-ticker,
per-month historical OPRA downloads via ThetaData. The script reads
`data/universes/tier{1,2}_*.csv` (51 tickers in the starter tier-2)
and writes parquet files to `data/historical/thetadata/`.

### 3.1 Workflow: ALWAYS dry-run first

```bash
uv run python scripts/download_tier2.py \
  --tier tier2_starter \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --max-tasks 5 \
  --dry-run \
  --skip-disk-check
```

The output should look like:

```
universe loaded: 51 ticker(s) from data/universes/tier2_starter.csv
--max-tasks 5 cap applied; 5 task(s)
state loaded: {'pending': 5, 'in_progress': 0, 'done': 0, 'failed': 0}

DRY RUN — 5 pending task(s)
  tier:        tier2_starter
  date range:  2024-01-01 .. 2024-01-31
  output dir:  data/historical/thetadata
  concurrency: 4

first 20 tasks:
  ABNB    2024-01
  ABNB    2024-02
  ...
```

Confirm:
- The right tickers appear (alphabetical first 20)
- The date range is correct
- Concurrency matches your ThetaData plan (Pro = 4 concurrent)

### 3.2 Workflow: small subset before full run

After dry-run looks right, run a small subset against a real key:

```bash
uv run python scripts/download_tier2.py \
  --tier tier2_starter \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --max-tasks 5 \
  --max-contracts 20
```

The `--max-contracts 20` flag caps each ticker to 20 contracts (out
of typically 100s–1000s). This keeps the subset run small (~50 MB,
~5 minutes) for verification.

After the subset run, inspect:

```bash
cat data/historical/.download_state.json | jq '.progress_summary'
# {"done": 5, "pending": 0, "in_progress": 0, "failed": 0}

cat data/historical/.manifest.json | jq '.summary'
# {"total_tickers": 51, "total_months": 1, "total_tasks_done": 5, ...}

# Per-contract layout:
ls data/historical/thetadata/EXP*/AAPL/
# 2024-01.parquet
```

### 3.3 Workflow: full production run (manual operator approval)

**Only after the subset run validates, kick off the full run.** This
hits ThetaData's API hard for hours and produces ~10–100 GB of
parquet depending on date range × ticker count.

```bash
uv run python scripts/download_tier2.py \
  --tier tier2_starter \
  --start-date 2024-01-01 \
  --end-date 2024-12-31 \
  --concurrency 4
```

The script:
- Pre-flight disk-space check (`--skip-disk-check` to bypass)
- Resume on failure: re-running picks up where the state file left
  off; completed tasks aren't re-fetched
- One parquet file per (contract, month); ~50 MB typical for liquid
  contracts
- Atomic state checkpoints after every (ticker, month) task

**The agent in this repo NEVER auto-runs the full download.** It is
a manual operator action with cost + bandwidth implications.

### 3.4 Workflow: validate-only (no download)

If you've manually edited a parquet or want to regenerate
`.manifest.json`:

```bash
uv run python scripts/download_tier2.py \
  --tier tier2_starter \
  --start-date 2024-01-01 \
  --end-date 2024-12-31 \
  --validate-only
```

This walks the on-disk parquet tree and rebuilds the manifest
without contacting ThetaData. State file is unchanged.

---

## 4. Live observer mode

Phase 3.3.5 ships `python -m uoa_detector run --source live` for
real-time operation against UW's WebSocket flow stream. ThetaData
live mode is wired in `live/factory.py` but gated behind a Phase 4
CLI message (per-contract subscription enumeration is deferred).

### 4.1 UW-only live observer

> **Phase 3.9 warning:** the WebSocket URL below
> (`wss://api.unusualwhales.com/v1/ws`) has never been validated against
> the live API. The vendored spec documents
> `wss://api.unusualwhales.com/socket?token=…` with a `join` message
> instead. For the daily candidate list use
> `uv run python -m uoa_detector screener --source rest --live-tickers …`,
> which runs on live-verified REST endpoints only.

```bash
export UNUSUAL_WHALES_API_KEY="..."  # or in .env
uv run python -m uoa_detector run \
  --source live \
  --feeds unusual_whales \
  --live-tickers SPY,QQQ,AAPL,MSFT,TSLA \
  --output json \
  --output-file decisions.jsonl
```

This:
- Connects to `wss://api.unusualwhales.com/v1/ws`
- Subscribes to flow alerts on the listed tickers
- Runs the full pipeline (M37 relative premium, M38 cluster, M40
  sweep, scoring, labeling) per fused OptionsPrint
- Writes one NDJSON `SignalDecisionRecord` per emission to
  `decisions.jsonl`
- Logs structured events to stderr

Press Ctrl-C to stop. `LiveObserver` traps SIGINT, closes the WS
gracefully, and lets the pipeline drain in-flight events before
process exit.

### 4.2 Multi-feed: ThetaData + UW (Phase 4)

Phase 3.3.5's CLI rejects `--feeds thetadata,unusual_whales` with a
clear message pointing at Phase 4. Operators wanting ThetaData live
today can import the factory directly from a Python script —
`live/factory.build_live_sources()` accepts both feeds; only the
CLI glue (contract enumeration + snapshot resolver wiring) is
deferred.

---

## 5. Cost expectations

These figures are approximate and depend on subscription tier,
ticker liquidity, date range, and current vendor pricing. Verify
against the vendor pages before committing to a budget.

| Item | Cost / month | Notes |
|---|---|---|
| ThetaData Pro | ~$80 | Required. Includes ~unlimited API quota. |
| Unusual Whales API-Plus | ~$50 | Includes 120 req/min sustained. |
| Local disk (tier-2, 12 months) | ~50 GB | ~50 MB × 51 tickers × 12 months × per-contract |
| Local disk (tier-2, 24 months) | ~100 GB | Doubles linearly |
| Egress bandwidth | Negligible | Hit only at fetch time; live stream is < 1 MB/s |

The detector itself has zero ongoing infrastructure cost; it runs
locally or on a single VM. No cloud database / queue / streaming
service is required for current phases.

---

## 6. Troubleshooting

### 6.1 "THETADATA_API_KEY is not set"

The script / CLI can't find `THETADATA_API_KEY` in env or in `.env`.

Fix:
- Verify `.env` exists and contains the key.
- Check the variable name is exactly `THETADATA_API_KEY` (no
  typos, no quotes around the value in the `.env` file).
- If using `uv run`, the `.env` file in the repo root is loaded
  automatically. If running outside `uv run`, export the variable
  manually.

### 6.2 "UnusualWhalesAuthError: HTTP 401"

The bearer token UW returned is being rejected by their API.

Fix:
- Re-copy the token from the UW dashboard. Tokens occasionally
  rotate (when the user clicks "regenerate" or after billing
  events).
- Check API-Plus is still active on your account.
- Try the curl smoke from §1.2 to isolate UW vs the detector.

### 6.3 "ThetaDataTransientError after 3 attempts"

The Theta Terminal is not responding.

Fix:
- Check Theta Terminal is running (`ps aux | grep ThetaTerminal`).
- Check the Terminal log for connection errors.
- Restart Theta Terminal — the local proxy occasionally needs
  restarting after long uptimes.
- The detector's circuit breaker trips after 5 consecutive 5xx /
  network errors; re-running after fixing the Terminal resumes
  cleanly.

### 6.4 "circuit breaker is open after 5+ consecutive failures"

The breaker tripped because too many consecutive requests failed.
The detector refuses requests pre-flight for 30 seconds.

Fix:
- Wait 30 seconds; the breaker auto-resets on the next successful
  request.
- Investigate the underlying cause — usually ThetaData's Terminal
  is down, or a rotated UW key, or a network partition.
- For long-running live observer runs that flap, raise the
  reconnect_max_attempts in the profile if the cause is transient.
- UW accounting (Phase 3.9.3): HTTP 404/422 (unknown ticker, contract or
  route; invalid input) do NOT count toward the breaker and are mapped to
  no-data by the providers. HTTP 429 counts and is retried with backoff.
  A daily-limit 429 (`daily_request_limit_hit`) fails fast without retry.

### 6.5 Live observer: "reconnect exhausted after 5 attempts"

The WebSocket disconnected and 5 reconnection attempts all failed.
The live observer raises `ReconnectExhaustedError` and exits.

Fix:
- The default backoff is exponential up to 60s. If the upstream
  is down for hours, raise `live.live_reconnect_max_attempts` in
  the profile (e.g., to 30 attempts → ~30 min total backoff
  ceiling).
- Check vendor status pages for outages.
- During US equity market overnight (after 16:00 ET, before 09:30
  ET next trading day), UW emits very few flow alerts — this is
  not a reconnect storm. The connection stays open with empty
  frames; the detector emits zero records.

### 6.6 `live_market` smoke test always skips

The smoke at `tests/integration/test_live_observer_smoke.py`
requires both `UNUSUAL_WHALES_API_KEY` AND US equity market hours
(09:30–16:00 ET, Mon–Fri).

Fix:
- Export `UNUSUAL_WHALES_API_KEY` for the test run.
- Run during market hours. The skip message tells you which gate
  failed.

### 6.7 Tier-2 download: "INSUFFICIENT DISK SPACE"

The pre-flight estimator (50 MB × tickers × months × 1.5 safety
margin) detected less free space than needed.

Fix:
- Free up disk space.
- Or pass `--skip-disk-check` if you've separately verified you
  have enough space (the estimator can over-count for small
  --max-tasks runs).
- Or run on a different filesystem with `--output-dir`.

### 6.8 Tier-2 download: tasks fail with "delisted contract"

ThetaData returns 404 / empty for contracts that delisted or never
traded. The orchestrator marks the task `failed` and continues.

Fix:
- This is expected for stale tickers. Inspect
  `data/historical/.download_state.json` for the failed keys.
- Re-running is safe (idempotent); failed tasks retry but will
  re-fail unless the underlying issue resolves.
- For known-bad contracts, you can manually remove them from the
  state file (`tasks` dict) — the orchestrator won't re-attempt
  removed entries.

### 6.9 Manifest gap-detection lists weekend dates

`build_manifest(detect_gaps=True)` lists every calendar day with
no rows. This includes weekends + holidays — there is no
trading-calendar overlay in Phase 3.3.

Fix:
- This is expected. Operators inspect the gap list and confirm
  weekends + known holidays are accounted for. Real gaps (a
  trading day with no rows) indicate a fetch problem.
- A trading-calendar provider is on the Phase 3.4 roadmap;
  manifest will then auto-classify.

---

## 7. Data layout reference

```
data/
├── universes/
│   ├── tier1_anchor.csv                    # ~20 tickers (broad ETFs + mega caps)
│   └── tier2_starter.csv                   # ~51 tickers (single-name vol)
└── historical/
    ├── .download_state.json                # state-of-the-world: tasks done/failed
    ├── .manifest.json                      # post-validation summary
    └── thetadata/
        └── {contract_subdir}/              # e.g., EXP240216_C_00150000
            └── {TICKER}/                   # e.g., AAPL
                └── {YYYY-MM}.parquet       # one file per ticker × month

# Find all AAPL files:
find data/historical/thetadata -path '*AAPL/*.parquet'
```

Parquet schema is `RAWPRINT_PARQUET_SCHEMA` (Phase 3.2.2.1) — see
`src/uoa_detector/backtest/parquet_schema.py`. Loading via PyArrow:

```python
import pyarrow.parquet as pq
table = pq.read_table("data/historical/thetadata/EXP240216_C_00150000/AAPL/2024-01.parquet")
```

---

## 8. Railway runtime (Phase 5.0)

The live site is the FastAPI app in `webapp/`, run as one Railway service.
Railway deploys `main` after the Phase 5.0 switch
(`docs/phase-5.0-merge-acceptance.md` §3.14); before the switch it deploys
`phase-3`.

| File | Role |
|---|---|
| `railway.json` | `startCommand` `uvicorn webapp.main:app --host 0.0.0.0 --port $PORT`; `restartPolicyType` `ON_FAILURE`; `healthcheckPath` `/health`, `healthcheckTimeout` 120 |
| `Procfile` | fallback for Procfile-based builders: `web: uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000}` |
| `runtime.txt` | Python `3.12` |
| `pyproject.toml` | the web dependencies (`fastapi`, `uvicorn[standard]`, `jinja2`, `psycopg[binary]`, `python-multipart`) sit in the main dependency set, so a plain install is enough |

- **Health.** `GET /health` returns `{"ok": true}`. It is the only route
  without HTTP Basic auth, so the Railway healthcheck needs no credentials.
- **Access gate.** Every other route needs `WEB_AUTH_USER` /
  `WEB_AUTH_PASSWORD` (§9).
  - Wrong or missing credentials return 401 with `WWW-Authenticate`.
  - If either variable is unset or empty, those routes return 503 with no
    data (fail closed).
- **Background tasks.** The app lifespan starts two supervised tasks: the
  live worker (`webapp/worker.py`) and the gamma refresh loop
  (`webapp/gamma_live.py`).
  - They start only when `LIVE_TICKERS`, `UNUSUAL_WHALES_API_KEY` and
    `DATABASE_URL` are all set. Without them the app only serves stored
    signals.
  - A task that exits or raises is restarted after 30 s.
  - Details: `docs/MODULES.md`, "Non-pipeline consumers".
- **No schema change.** Phase 5.0 changes no schema: `StoredSignal` and the
  tables are identical on both lineages. A rollback to the last `phase-3`
  deployment is therefore data-safe (contract §3.11, §3.14).

**Local run** (no live tasks):
1. Set `WEB_AUTH_USER` and `WEB_AUTH_PASSWORD` in the shell.
2. Run:

   ```bash
   uv run uvicorn webapp.main:app
   ```

Without `DATABASE_URL` the app reads `sqlite:///webapp/seed.db`. That file is
not in git; `scripts/seed_screener.py` builds it from local replay data.

---

## 9. Environment variables

Names only. Values live in `.env` locally and in Railway variables in
production. `.env.example` lists the names with blank values.

| Variable | Used by | Needed | Behaviour |
|---|---|---|---|
| `DATABASE_URL` | `webapp/repo.py`, `webapp/gamma.py`, `webapp/journal.py`, `webapp/worker.py`; default `--dest` of `scripts/migrate_to_postgres.py` | Railway | Postgres in production; the webapp default is `sqlite:///webapp/seed.db`. The worker rewrites `postgres://` / `postgresql://` to `postgresql+psycopg://`, and does not start without this variable. |
| `LIVE_TICKERS` | `webapp/worker.py` | opt-in | Comma-separated, upper-cased. Unset or empty: no live worker and no gamma refresh. |
| `UNUSUAL_WHALES_API_KEY` | `config/credentials.py` (`Credentials`), `webapp/worker.py`, `webapp/pricing.py`, CLI `screener --source rest`, `scripts/run_m28_overnight.py` | any UW call | Without it the worker does not start and journal prices stay empty. |
| `WEB_AUTH_USER`, `WEB_AUTH_PASSWORD` | webapp access gate (Phase 5.0.9) | Railway | Fail closed: unset or empty returns 503 on every route except `/health`. Constant-time comparison. |
| `LIVE_POLL_INTERVAL_S` | `webapp/worker.py` | optional | Default 60. The worker uses `max(60.0, value)`, so a value below 60 s has no effect. |
| `LIVE_MIN_PREMIUM` | `webapp/worker.py` → `UnusualWhalesFlowPollSource` | optional | Default 25000 (USD). Polled alerts below it are skipped. |
| `PORT` | `railway.json` / `Procfile` start command | set by Railway | `Procfile` falls back to 8000. |
| `UOA_LOG_LEVEL` | `config/__init__.py` (`AppSettings.log_level`), CLI logging | optional | Default `INFO`. `WARNING` keeps full-universe replays fast. `AppSettings` also reads `UOA_PROFILES_DIR` and `UOA_DEFAULT_PROFILE_FILENAME`. |
| `THETADATA_API_KEY`, `THETADATA_USERNAME` | `Credentials` | ThetaData adapter only | Not used by the webapp. |

---

## 10. Unusual Whales request budget

- **Daily limit:** 30,000 requests per day, per the `x-uw-token-req-limit`
  response header observed on 2026-09-14 (§1.2).
- **Rate cap:** 1.5 requests per second (90/min), set in `profiles/v5_default.yaml`
  (`data_sources.unusual_whales.rate_limit_requests_per_second`).
  Accepted in `docs/phase-5.0-merge-acceptance.md` §3.4.
- **Client behaviour** (`sources/unusual_whales/client.py`, Phase 3.9.3):
  - a plain 429 is retried with backoff, then raises
    `UnusualWhalesRateLimitError`;
  - a 429 whose body carries `daily_request_limit` raises
    `UnusualWhalesDailyLimitError` at once, with no retry.
- **Reference load:** `x-uw-daily-req-count` was 4,613 at 18:32 UTC on
  2026-09-14, with 10 live tickers (contract §3.14).

Guards in the live tasks:

| Guard | Where | Value |
|---|---|---|
| RTH-only polling | `sources/market_hours.py::is_market_open`, checked by `UnusualWhalesFlowPollSource.stream` and `gamma_refresh_loop` | Weekdays 13:30–21:00 UTC. This covers both EDT and EST; holidays are not modelled. Outside the window both tasks sleep 300 s between checks. |
| Poll floor | `webapp/worker.py::live_config_from_env` | `max(60.0, LIVE_POLL_INTERVAL_S)` seconds between polling rounds |
| Daily-limit backoff | `sources/unusual_whales/flow_poll.py` (`daily_limit_backoff_s`) | 1800 s (30 min) after a daily-limit error. Detected by type (`UnusualWhalesDailyLimitError`); the `daily_request_limit` message substring is the fallback. |
| Gamma refresh cadence | `webapp/gamma_live.py::gamma_refresh_loop` (`interval_s`) | 240 s between refresh rounds |

Per round, the flow poller spends one request per ticker. The gamma refresh
spends four direct requests per ticker (`greek-exposure/strike`, `iv-rank`,
`earnings`, `ohlc/1d`), plus the catalyst provider's calls.

---

## 11. ThetaData bulk and derived-data scripts

These scripts built the local research data behind the Phase 3.5.5/3.6
replay runs and the 4.x studies. `scripts/` is ruff-only and outside
`mypy --strict` (`docs/phase-5.0-merge-acceptance.md` §3.11). The data
windows they produced are burned (`docs/INDEX.md` §6).

| Script | Phase | What it does (from its docstring) | Output |
|---|---|---|---|
| `scripts/download_bulk.py` | 3.5.3.9 | Bulk historical download. One ThetaData v3 `trade_quote` call with `expiration=*` per ticker-day returns the whole chain, each trade with its at-trade bid/ask. Replaced the per-contract `scripts/download_tier2.py` driver. | `data/historical/bulk/{TICKER}/{YYYY-MM}.parquet` |
| `scripts/download_chain_snapshots.py` | 3.6.2 | Daily chain snapshots for the self-derived GEX, IV and OI providers. It joins full-chain daily OI with eod bid/ask, Black-Scholes-inverts IV from the eod mid, and takes parity spot from the bulk data. Requires Theta Terminal running locally. | `data/chain_snapshots/{TICKER}.parquet` |
| `scripts/compute_spot_series.py` | 3.6.5 | Per-ticker minute-bar spot series (mean parity spot per minute) from the bulk parquet, for M23. | `data/spot_series/{TICKER}.parquet` |
| `scripts/compute_medians.py` | 3.5.5.4 | Per-ticker median trade premium from the bulk parquet, for M37. | `data/medians_bulk.csv` |
| `scripts/fetch_earnings_calendar.py` | 3.6.6 | Earnings dates for the download universe from Yahoo Finance, for M22. `yfinance` is a build-time tool, not a runtime dependency. | `data/earnings_calendar.csv` |

- Gitignored: `data/chain_snapshots/`, `data/spot_series/` and the bulk
  parquet.
- Committed: `data/medians_bulk.csv` and `data/earnings_calendar.csv`. Both
  are backtest-only; live scoring never reads them (contract §4).
- Bulk download runbook: `docs/phase-3.5.3-config.md`.

---

## 12. Where to look next

- **Current truth, burned data windows, Phase 5.x registry**: `docs/INDEX.md`
- **Current UW endpoints**: `docs/phase-3.9-uw-endpoint-correction-acceptance.md` §3
- **Adapter design + judgment calls**: `docs/phase-3.3-acceptance.md`
- **Backtest harness**: `docs/BACKTEST.md`
- **Per-module docstrings**: every adapter / provider / pipeline file
  has a top-level docstring with the design decisions and the test
  pins for each.

For Phase 3.3 specifically, every sub-phase commit message in `git
log` lists the decisions made in that commit (search for `decision (`
in commit bodies).
