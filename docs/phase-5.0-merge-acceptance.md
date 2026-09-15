# Phase 5.0 — `main` ← `phase-3` unification (acceptance contract)

Status: **FROZEN** (per D1). Approved by Berkay on 2026-09-14, including §3.8
(live-worker degradation), §3.9 (`ohlc/1d` fix), the §3.13 CLAUDE.md rule text
(alerts notify-only, virtual portfolio log-only) and the §8 order.
Owner: Berkay. Author: Claude (Opus 5).

---

## 1. Objective

One branch, `main`, carries both lineages and is what Railway deploys.
Nothing that works on the live site today may regress.

After 5.0, `main` holds:
- the Phase 3.9 live-verified Unusual Whales layer;
- phase-3's webapp, live worker, studies and replay/verdict engine;
- a CI gate, a deploy healthcheck and an access gate on the site;
- UI copy that makes no edge claim, and one index that states current truth
  across both lineages.

The terminal modules are Phase 5.1+ (§8). None of them is in 5.0.

## 2. Refs and recorded decisions

| Ref | Commit | Note |
|---|---|---|
| merge-base | `b7de170` | |
| `origin/main` | `27e2842` | PR #7, Phase 3.9 |
| `origin/phase-3` | `f0d469f` | Phase 4.43, the replay-safe live store |
| trial merge | `f88e064` | local branch `trial-merge-5.0`, built on phase-3 `a514b92` |

**Trial result.** On the trial merge, pytest gives 2146 passed / 30 skipped
(all key-gated). `mypy --strict src/` is clean on 131 files and `ruff check .`
is clean. Git flagged 19 conflicts. Three further breaks merged with no
conflict marker, and the gate caught all three:
- a duplicate `ParquetExitQuoteProvider` export;
- trade-producer arity;
- a lost `StoredSignal` import.

**Phase 4.43.** It landed on phase-3 after the trial. It touches only
phase-3-only paths: `backtest/sqlite_store.py`, `webapp/worker.py` and
`tests/unit/test_live_store_replay_safe.py`.

Berkay's decisions of 2026-09-14 are pinned in §3:
- ship hotfix 4.43 on phase-3 first (done);
- approve every recommendation of the 5.0 decision table;
- switch Railway directly, after the US close;
- add HTTP Basic auth in 5.0.

## 3. Decisions

### 3.1 Merge mechanics

- Run `git merge --no-ff origin/phase-3` on branch `phase-5.0-merge`, cut from
  `origin/main`. The full phase-3 ancestry is kept.
- phase-3 history is not D2-clean (e.g. `2fed891` "TEMP surface tracebacks",
  `554ba90`). Bisect on `main` therefore uses `git bisect --first-parent`.
- The merge commit itself must be green.
- The merge commit carries exactly the resolutions of §3.2. No other edits.
- **Verification.** `git diff trial-merge-5.0 <merge commit>` must show exactly
  the Phase 4.43 diff and nothing else.

### 3.2 Resolution table (replay of trial `f88e064`)

**Take main** (15 paths):
- UW client: `sources/unusual_whales/client.py`
- the six UW providers: `catalyst_calendar`, `dark_pool`, `dealer_gamma`,
  `iv_history`, `open_interest`, `sector_peer`
- tests:
  - `tests/unit/test_unusual_whales_client.py`
  - `tests/unit/test_unusual_whales_providers_part1.py`
  - `tests/unit/test_unusual_whales_providers_part2.py`
  - `tests/unit/test_catalyst_window.py`
  - `tests/unit/test_live_factory.py`
  - `tests/integration/test_cli_live.py`
- docs: `docs/MODULES.md`, `docs/phase-3.5.1-validation.md`

**Manual** (6 paths):

| Path | Resolution |
|---|---|
| `.gitignore` | union of both blocks |
| `backtest/cell_runner.py` | keep both engines. Widen `synthetic_trade_producer` and the `replay_trade_producer` closure to the 4-argument producer (`data: BacktestDataHandle \| None = None`). Restore the `StoredSignal` TYPE_CHECKING import. |
| `cli.py` | `run-4cell --trades noop\|historical\|synthetic\|replay`. Keep all options from both sides and `data_handle=data_handle`. Widen `_TradeProducer` to 4 arguments. |
| `backtest/__init__.py` | drop phase-3's `ParquetExitQuoteProvider` export (silent break) |
| `tests/integration/test_cli_run_4cell.py` | main's body, plus asserts for `synthetic` and `replay` |
| `tests/unit/test_parquet_exit_quote.py` | import phase-3's class by module path (silent break; assertions untouched) |

**Everything phase-3-only arrives verbatim:**
- `webapp/`, `Procfile`, `railway.json`, `runtime.txt`
- `pyproject.toml`, `uv.lock`
- `scripts/`
- `sources/thetadata_derived/`, `flow_poll.py`, `market_hours.py`
- `falsification.py`, `sanity_audit.py`, `parquet_exit_quote.py`
- `sqlite_store.py`, including 4.43
- the orchestrator empty-store fix `22178e3`
- the phase-3 docs, `profiles/v6_thetadata_confluence.yaml` and `data/*`

### 3.3 Unusual Whales layer

- `main`'s 3.9 client and providers win.
- 17 phase-3-only provider unit tests leave the spec (D10). The merge commit
  body lists each one with the main test that replaces it.

### 3.4 Profile `profiles/v5_default.yaml` (data_sources only)

- phase-3's operational transport caps are accepted:

  | Setting | Value |
  |---|---|
  | UW `rate_limit_requests_per_second` | 1.5 |
  | ThetaData `rate_limit_requests_per_second` | 25.0 |
  | ThetaData `historical_concurrency` | 8 |

- No scoring block changes: `scoring.*`, penalties, labels, risk buckets, DTE,
  cluster and backtest are byte-identical on both sides (D4/D8 untouched).
- The paired asserts in `test_unusual_whales_settings.py` and
  `test_thetadata_settings.py` follow the YAML.
- This supersedes the 3.9 contract §4 non-goal. That doc is not edited.
- Runs after the merge carry a new profile content hash.

### 3.5 Fusion contract

- phase-3 `012bde2` is accepted: IV and OI are optional on `OptionsPrint`, and
  fusion no longer raises on their absence. `test_no_source_supplies_iv_raises`
  and `test_no_source_supplies_oi_raises` leave the spec (D10).
- phase-3 `42f7672` is accepted: per-contract download tasks are best-effort.
- `rest_flow.py` still drops IV/OI-less prints. Its stale rationale comment is
  corrected in the docs commit; behaviour is unchanged.

### 3.6 Backtest engines

- Both engines stay:

  | Mode | Origin | Exit quote |
  |---|---|---|
  | `--trades historical` | main 3.5.0 | same-UTC-day |
  | `--trades replay` | phase-3 3.5.5 | 3-month walk-back; produced the 3.6 EDGE REJECTED verdicts |

- The package-level `uoa_detector.backtest.ParquetExitQuoteProvider` is main's
  class. phase-3's class is imported by module path.
- Verdicts from the two modes are not comparable. Every report or verdict
  names its mode.
- No verdict re-run, engine retirement or exit-quote rule change in 5.0
  (registry, §8).

### 3.7 Frozen-doc edits made on phase-3 (ratified here, not by editing)

- `docs/phase-3.5-acceptance.md`, commit `2224077`: the sample-size gate
  (`closed_trades >= 30`, otherwise INSUFFICIENT) is **ratified retroactively**.
  The 3.6 verdict was computed under it.
  - D8 debt: `falsification.py` hardcodes 30 / 0.5 / 0.75. Moving them is a
    registry item (§8).
- `docs/phase-3.3-acceptance.md`, commit `73234b8`: the appended 3.3.8/3.3.9
  follow-up sections are **accepted** (addendum pattern). Their endpoint facts
  are superseded by 3.9.
- `docs/phase-3.6-acceptance.md`, commits `d351eec` (before code) and `22dbf56`
  (after 3.6.1 code): **recorded** as a historical D1 breach.
  - The 3.6 verdict stands: both edits made the test stricter or more
    accurate, and neither tuned on results.

### 3.8 Live worker

1. **Replay-safe store** (4.43) arrives with the merge.
   - `_open_live_store` keeps `flush_threshold=1, replay_safe=True`.
2. **flow_poll** stays on per-ticker `/api/stock/{t}/flow-alerts` for parity:
   it is prod-proven with 10 live tickers.
   - It gains typed `UnusualWhalesDailyLimitError` detection; the substring
     fallback stays.
   - The port to `/api/option-trades/flow-alerts` is not in 5.0 (registry). It
     changes the event-id scheme and starts firing the wide_spread penalty on
     live cards.
3. **Degradation (new, needs explicit approval with this doc).**
   - *Problem.* main's providers propagate transient UW errors (429 after
     retries, daily limit, 5xx, open breaker). Only M23 catches them. On main,
     every transient error would abort `pipeline.run` and restart the worker:
     30 s gap, fresh client, 10-minute replay.
   - *Wrappers.* A live-worker-only degrading wrapper is added per provider
     protocol (M21, M22, M24, M25, M26, M27). It maps `UnusualWhalesRateLimitError`
     (including the daily limit), `UnusualWhalesTransientError` and
     `CircuitBreakerOpenError` to that protocol's documented no-data return.
     Each catch logs a WARNING and increments a counter.
   - *Still loud:* `UnusualWhalesAuthError` (a bad key must never look like no
     data) and programming errors (`TypeError`/`ValueError`).
     `UnusualWhalesNotFoundError` already maps to no-data inside the providers.
   - *Builder switch.* `build_live_stage_pipeline(client, profile, *,
     degrade_transient_errors=False)` gains the keyword. The default leaves the
     screener and backtests unchanged. The worker switches from
     `cell_runner.fusion_stages_with_uw` to
     `build_live_stage_pipeline(..., degrade_transient_errors=True)`: same
     stages and order, one shared catalyst provider.
   - `fusion_stages_with_uw` stays for the replay path.
   - Stages are untouched (frozen 3.4). Until the stage-level `provider_error`
     branch lands (registry), decision records show the no-data branch.

### 3.9 Journal pricing and vol-board realized vol (bug, found 2026-09-14)

**Live probe.** `GET /api/stock/SPY/ohlc/1d` returns 752 rows, **newest first**
(`2026-09-14` … `2025-09-15`). Each date has several rows, with `market_time`
∈ {`pr`, `r`, `po`}.

| Function | What it does now | Effect |
|---|---|---|
| `webapp/pricing.latest_close` | takes `data[-1]` | the oldest close, about one year stale. Journal underlying and SPY prices, and therefore the market-neutral excess, are wrong. |
| `webapp/gamma_live._realized_vol` | takes `closes[-21:]` | the oldest 21 rows, mixing sessions. Vol-board realized vol and VRP are wrong. |

**Fix.**
- Use regular-session rows only (`market_time == "r"`), ordered by `date`
  explicitly.
- The latest close is the newest regular close. Realized vol uses the last 21
  regular closes.
- Tests use a trimmed newest-first live fixture.

**Prod data.** Two journal trades exist, both open and logged 2026-06-25, both
with stale entry prices.
- They are not rewritten; journal rows are forward evidence.
- The closeout flags them for Berkay.

### 3.10 Access gate

- HTTP Basic auth on every route except `GET /health`.
- Credentials come from `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`; `.env.example`
  lists the names only. The comparison is constant-time.
- One shared pair for Berkay and his friend until 5.1 login.
- **Fail closed:** if either variable is unset or empty, every non-health route
  returns 503 with no data.
- Wrong or missing credentials return 401 with `WWW-Authenticate`.
- Berkay sets both Railway variables before the switch. Values never appear in
  chat or the repo.

### 3.11 Deploy safety

- **`railway.json`:** `"healthcheckPath": "/health"`, `"healthcheckTimeout": 120`;
  `restartPolicyType` stays `ON_FAILURE`.
- **CI:**
  - `.github/workflows/ci.yml` runs on push and PR: `uv sync --locked`, the
    hermetic gate, `uv lock --check`.
  - Once the check has run on the PR, protect `main` so it requires that check
    (`gh api`).
  - Railway "Wait for CI" is a Berkay action.
- **mypy:** extended to `webapp/`. The 21 known errors in 5 files are fixed
  mechanically. `scripts/` stays ruff-only, a documented exception (research
  record, 80 errors).
- **No schema change in 5.0.** `StoredSignal` and the tables are identical on
  both lineages, so a rollback to `phase-3-final` stays data-safe.

### 3.12 Honest copy

**Webapp copy** that implies a validated or tradeable edge becomes context-only:
- `_signals.html` ("The research edge is to sell that rich vol")
- `dashboard.html` ("The one research-validated edge is the vol-premium read")
- `gamma.html`
- `GammaContext.vol_label`: "vol-selling candidate" / "vol-buying lead" become
  descriptive, e.g. "IV rich vs realized" / "IV cheap vs realized".

**README.** The "+0.77 Sharpe *validated*" claim is superseded by Study D's
out-of-sample WEAK result.

**Canonical edge statement**, used verbatim in `docs/INDEX.md` §0, the README
and CLAUDE.md:

> No tradeable edge found. Directional UOA confluence (UW-free, v6):
> REJECTED (phase-3.6-closeout). Gamma-regime directional and pinning:
> REJECTED (4.9, 4.11). Vol premium: untradeable after costs
> (edge_to_money.md). Conditioning replication: WEAK (study_D_result.md).
> UW-fed Track B (v5_gamma_squeeze with real UW enrichment) has never been
> testable, because UW history is about 7 days (phase-3.5.5-status B1).

Whether UW-fed Track B is closed or open is not decided here (§8).

### 3.13 Docs

- **No renames or moves.** Code, tests and scripts reference more than 30 doc
  paths.
- **No frozen-doc edits.**
- **New `docs/INDEX.md`:**
  - §0 canonical truth;
  - the living references;
  - a shared-history table and one lineage table per branch;
  - a collision table: 3.3.9 vs 3.9; 3.5.0 vs 3.5.4/3.5.5; 3.5.1; 3.6.x; the
    tag `phase-3.6-complete`; "screener" meaning the CLI vs the web UI; the
    phantom `docs/phase-3.5-results.md`; the "Phase 5 (paper trading)" label in
    the frozen 3.5 contract;
  - burned data windows;
  - the 5.x registry.
- **New `docs/phase-3.5.1-validation-record.md`:** a verbatim copy of phase-3's
  KAPALI record. The original path keeps main's runbook.
- **Living docs:**
  - `MODULES.md`: UW provider lineage note, self-derived ThetaData providers,
    non-pipeline consumers.
  - `DATA_INTEGRATION.md`: Railway runtime and variable names, UW 30k/day
    budget.
  - `BACKTEST.md`: merged `run-4cell` modes, verdict engine, sanity audit.
  - `.env.example`: variable names without values.
- **Pin test** for `profiles/v6_thetadata_confluence.yaml`. The profile itself
  is unchanged; the track stays burned.
- **CLAUDE.md** (the rule text needs explicit approval with this doc):
  - current state and gate counts;
  - commit flow becomes feature branch → PR → `main`. Railway deploys `main`
    after the switch. No commits to `phase-3` after the freeze.
  - the reading list starts with `docs/INDEX.md`, then the 3.9 contract and
    closeout, the 3.6 closeout, `edge_to_money.md` and `study_D_result.md`;
  - "Phase 4+ territory; we are nowhere near" is replaced by:
    - the webapp is live decision-support; nothing executes, and there is no
      broker link;
    - alerts (notify-only) and a virtual portfolio (log-only paper positions,
      never executed) are approved product modules as of 2026-09-14, each
      behind its own acceptance doc;
  - D11 is corrected to name the two existing TODOs
    (`calibration/resolver.py:48`, `pipeline/stages/__init__.py:4`), with a
    registry item to remove them;
  - the phantom `docs/phase-3.5-results.md` reference is replaced by
    `docs/INDEX.md` §0;
  - the gate line and the Phase 5.x numbering rules are updated.

### 3.14 Cutover (direct, post-close; no staging)

**Pre-switch checklist.** All must hold:
1. Berkay merged the PR into `main` (merge commit, no squash).
2. The gate is green on `main` HEAD, locally and in CI.
3. Key-gated live checks pass:
   `set -a; source .env; set +a; uv run pytest -m integration tests/integration/test_uw_live_endpoints.py`
   gives 10/10.
4. Local smoke, with uvicorn on sqlite and the auth variables set:
   - `/health` returns 200;
   - other routes return 401 without credentials and 200 with them.
5. Tag `phase-3-final` is set at the `origin/phase-3` tip and pushed.
6. Backup taken:
   - `pg_dump -Fc` of `backtest_run`, `signal`, `backtest_run_error` and `trade`,
     stored outside the repo;
   - row counts recorded (baseline: `signal` 15,271 rows, 27 MB; `trade` 2 rows).
7. Berkay has set `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` on Railway.

**Window.** A weekday after 21:00 UTC, or a weekend.
- No RTH overlap between old and new deployments.
- The next session's `live-YYYY-MM-DD` run is written by main only.

**Switch.**
- Berkay changes the Railway source branch from `phase-3` to `main`. No other
  variable changes.
- Claude watches the deploy status, `/health`, and the logs (no ImportError or
  Traceback loop), then checks `/`, `/gamma` and `/journal` with credentials.

**First-session soak** (next RTH):
- the LIVE badge stays fresh (< 15 min);
- `live-*` signal count keeps rising;
- no restart loop;
- gamma refresh reports `N/N tickers`;
- `x-uw-daily-req-count` stays under 30,000. Reference: 4,613 at 18:32 UTC on
  2026-09-14 with 10 live tickers.

**Rollback triggers:**
- `/health` non-200 for more than 2 minutes;
- a crash loop;
- no signals 30 minutes into RTH while UW flow exists.

Rollback action: Railway → redeploy the last phase-3 deployment and set the
source back to `phase-3`. This is data-safe because §3.11 excludes schema
changes.

**Freeze.**
- From the merge commit on, `phase-3` takes emergency hotfixes only. Each one
  also lands on `phase-5.0-merge`.
- After one clean session on `main`, `phase-3` is locked via branch protection.

## 4. Non-goals (not changed in 5.0)

- No scoring-threshold change. The scoring blocks of `v5_default`,
  `v5_gamma_squeeze` and `v6` are untouched.
- No verdict re-run, engine retirement, or exit-quote rule change.
- No flow_poll endpoint port. No `gamma_live` units change: `net_gex` is share
  gamma, and the board only uses its sign, flip and walls, which are
  unit-invariant. No gamma-loop client reuse.
- No stage-level `provider_error` branch in the frozen 3.4 stages.
- No Alembic or schema change, no dedicated Postgres move, no retention job.
- None of the following, all 5.1+: login, UW budget governor, advice-language
  lint, SSE, terminal screens, daily ideas, AI assistant.
- 3.9 closeout §7 flags (M21 deep-OTM flip, FOMC typed `report`, M23 EST/EDT
  clamp, M25 share classes, M26 notional truncation, point-in-time membership,
  WS URL) go to the registry.
- `data/medians_bulk.csv` and `data/earnings_calendar.csv` stay backtest-only.
  They are never used in live scoring.

## 5. Tests

**New:**
- flow_poll: `UnusualWhalesDailyLimitError` without the body marker triggers
  the 1800 s backoff.
- Degrading wrappers:
  - each wrapper, for RateLimit, DailyLimit, Transient and BreakerOpen, returns
    no-data and increments its counter;
  - NotFound results pass through; AuthError and ValueError propagate;
  - pipeline level: a fake 503 still yields a `PipelineResult`;
  - with the builder default, errors still propagate.
- Worker wiring: `run_live_worker` hands `Pipeline` the degrading
  `build_live_stage_pipeline` stages (stubbed, as in
  `test_gamma_live_loop_wiring.py`). It keeps the replay-safe store.
- Pricing and vol: with a trimmed live `ohlc/1d` fixture (newest first,
  pr/r/po rows), `latest_close` is the newest regular close and realized vol
  uses the last 21 regular closes.
- Auth:
  - every route in `app.routes` except `/health` requires credentials
    (parametrized, so new routes are covered);
  - wrong credentials return 401 plus `WWW-Authenticate`;
  - unset variables return 503;
  - `/health` stays open.
- `v6_thetadata_confluence` profile pin.

**D10 record** (existing tests removed or changed; each named in its commit
body):
- 17 phase-3 UW provider tests (§3.3);
- 2 main fusion-raises tests (§3.5);
- phase-3 `42f7672`'s removed download test;
- `test_cli_run_4cell` union asserts;
- `test_parquet_exit_quote` import path;
- webapp route tests gain a credentials fixture, with assertions unchanged;
- any assertion on the old `vol_label` strings.

**Gate per commit:**
- up to and including 5.0.6:
  `env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ && uv run ruff check .`,
  plus `uv lock --check` from 5.0.3;
- from 5.0.7: the same, with `uv run mypy --strict src/ webapp/`.

## 6. Done when

- `main` contains the §7 commits, each green on its gate, and CI is green on
  the PR.
- `test_uw_live_endpoints.py` passes 10/10 with the key.
- Railway deploys `main`, and the first RTH session meets the §3.14 soak
  criteria without a rollback.
- `docs/phase-5.0-closeout.md`, a separate file, holds the paket-mode report.
  It lists the two stale journal rows and anything this contract did not
  anticipate.

## 7. Commit plan

| # | Scope |
|---|---|
| 5.0.1 | this contract |
| 5.0.2 | CI workflow |
| 5.0.3 | merge `origin/phase-3` (replay of `f88e064` plus 4.43; D10 list in body) |
| 5.0.4 | flow_poll typed daily-limit detection |
| 5.0.5 | degrading providers + worker → `build_live_stage_pipeline` |
| 5.0.6 | `ohlc/1d` ordering fix (journal pricing, realized vol) |
| 5.0.7 | `webapp/` under `mypy --strict`; gate extended |
| 5.0.8 | `railway.json` healthcheck |
| 5.0.9 | HTTP Basic auth, fail closed |
| 5.0.10 | honest copy |
| 5.0.11 | v6 profile pin test |
| 5.0.12 | `docs/INDEX.md`, 3.5.1 validation record, MODULES / DATA_INTEGRATION / BACKTEST / `.env.example` |
| 5.0.13 | README + CLAUDE.md |
| 5.0.14 | closeout |

## 8. Phase 5.x registry

Numbers are allocated here and tracked in `docs/INDEX.md`. Every phase needs its
own acceptance doc before code.

| Phase | Scope |
|---|---|
| 5.1 | **Foundation:** login (two users, no signup), Alembic for webapp tables with a prod stamp (additive only), append-only table policy, UW budget governor + `/health/budget`, advice-language lint, disclaimer, deeper `/health` |
| 5.2 | Terminal main screen (renders from Postgres only; zero UW calls per view) |
| 5.3 | Ticker detail |
| 5.4 | Live stream (SSE) |
| 5.5 | Watchlist + alerts (notify only) |
| 5.6 | Virtual portfolio / forward record (log-only paper positions, never executed) |
| 5.7 | Daily ideas: pre-registered, logged and forward-scored against a random control. Presented as unvalidated ideas, never as a recommendation. |
| 5.8 | AI assistant (read-only DB tools, no direct UW tool, per-user cost cap) |
| 5.9 | Landing, methodology, glossary, legal |
| 5.10–5.19 | Engine and data fixes, in registry order. First candidates: flow_poll → `/api/option-trades/flow-alerts`; stage-level `provider_error`; gamma_live units and client reuse; exit-quote staleness rule and engine retirement; falsification gates into the profile (D8); 3.9 §7 flags; D11 TODO removal; dedicated Postgres, retention and backups. |
| 5.20+ | Research studies (`preregister_<L>.md`, next letter E; non-burned windows only) |

**Hotfixes.** A hotfix to a live component during 5.x is a numbered sub-commit
of the owning phase, with "hotfix" in the summary.

**Open items for Berkay** (none blocks 5.0):
- whether the UW data licence allows showing data to a second person and
  sending it to the Anthropic API (gates the friend login and 5.8);
- the AI model and monthly cost cap;
- daily-ideas pre-registration parameters;
- alert channel and UI language;
- ratification of the 3.5.0 fixture-path judgment call;
- the status of UW-fed Track B;
- relabelling the seed run;
- Berkay's local untracked `data/historical/` manifests.
