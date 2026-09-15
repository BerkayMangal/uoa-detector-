# Phase 5.0 — `main` ← `phase-3` unification (closeout)

**Status: CODE COMPLETE — OPEN until the Railway switch and the first RTH
session on `main` meet the soak criteria** (contract §6). The contract is
`docs/phase-5.0-merge-acceptance.md` and is not edited. Every deviation found
while implementing it is recorded in §5 below.

Branch `phase-5.0-merge`. First-parent series on top of `origin/main` `27e2842`.

## 1. Commits

| # | Commit | Scope |
|---|---|---|
| 5.0.1 | `153d891` | acceptance contract |
| 5.0.2 | `0d470c9` | CI gate workflow |
| 5.0.3 | `6d1e4ca` | merge `origin/phase-3` (`f0d469f`); D10 list in commit body |
| 5.0.4 | `305ebf1` | flow_poll typed daily-limit detection |
| 5.0.5 | `be283a5` | degrading UW providers + live worker on `build_live_stage_pipeline` |
| 5.0.6 | `18d143b` | ohlc/1d ordering fix (journal pricing + vol-board realized vol) |
| 5.0.7 | `571b100` | `webapp/` under `mypy --strict`; gate extended |
| 5.0.8 | `ad594ab` | `railway.json` healthcheck |
| 5.0.9 | `0a51a58` | HTTP Basic auth, fail closed |
| 5.0.10 | `f8f2590` | honest copy |
| 5.0.11 | `f320f08` | v6 profile pin test |
| 5.0.12 | `4b4f43c` | docs: INDEX, 3.5.1 validation record, living references, stale code text |
| 5.0.13 | `06f7f37` | README positioning + CLAUDE.md rules |
| 5.0.14 | `0648e4b` | closeout |
| 5.0.15 | this commit | CI job env: plain CLI output under GitHub Actions |

## 2. Gate

Every commit was gated in isolation: a detached worktree at that commit,
`uv sync`, then the gate. The mypy scope was read from that commit's own CI
file.

| # | Commit | pytest (passed / skipped) | mypy --strict (scope, files) | ruff | uv lock --check |
|---|---|---|---|---|---|
| 5.0.1 | `153d891` | 1920 / 30 | `src/`, 117 | clean | from 5.0.3 (contract §5) |
| 5.0.2 | `0d470c9` | 1920 / 30 | `src/`, 117 | clean | consistent |
| 5.0.3 | `6d1e4ca` | 2150 / 30 | `src/`, 131 | clean | consistent |
| 5.0.4 | `305ebf1` | 2151 / 30 | `src/`, 131 | clean | consistent |
| 5.0.5 | `be283a5` | 2290 / 30 | `src/`, 132 | clean | consistent |
| 5.0.6 | `18d143b` | 2305 / 30 | `src/`, 132 | clean | consistent |
| 5.0.7 | `571b100` | 2305 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.8 | `ad594ab` | 2305 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.9 | `0a51a58` | 2541 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.10 | `f8f2590` | 2541 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.11 | `f320f08` | 2548 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.12 | `4b4f43c` | 2548 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.13 | `06f7f37` | 2548 / 30 | `src/ webapp/`, 144 | clean | consistent |
| 5.0.14 | `0648e4b` | 2548 / 30 | `src/ webapp/`, 144 | clean | consistent |

Gating:
- 5.0.1–5.0.3 were gated before they were committed.
- 5.0.4–5.0.13 were re-gated after being cherry-picked into this order.
- 5.0.14 was gated before it was committed. 5.0.15 changes only the CI
  workflow and this document; its gate result is in its commit message, and
  CI on PR #8 runs against it.
- Every skip is a key-gated integration test.
- The two pytest warnings predate 5.0: a Starlette/httpx deprecation, and a
  numpy divide in a gamma test.

At HEAD: 2548 passed / 30 skipped. `mypy --strict src/ webapp/` is clean on 144 source files, ruff is clean and `uv lock --check` is consistent. Collected tests went from 2150 at the merge commit to 2548; most of the new ones are parametrized auth and degradation cases..

**Live and key-gated checks:**
- `tests/integration/test_uw_live_endpoints.py`: 10/10 passed on `6d1e4ca`,
  2026-09-14 ~18:55Z, with Berkay's key.
- Webapp boot smoke on `6d1e4ca` (empty sqlite, no live worker): `/health`,
  `/`, `/journal`, `/journal/new` and `/gamma` all returned 200. Auth landed
  later, in 5.0.9; its route coverage is in `test_webapp_auth.py`.
- Phase 4.43 production check (phase-3 `f0d469f`, redeployed during RTH at
  18:32Z):
  - page LIVE, today's run 173 → 187 signals;
  - run counter 187 = 187 stored rows, i.e. replayed alerts were skipped and
    not counted;
  - no Traceback, IntegrityError or restart loop in the logs.

## 3. New tests

- 5.0.4: `test_uw_flow_poll.py::test_typed_daily_limit_error_triggers_long_backoff`.
- 5.0.5:
  - `test_uw_degrading_providers.py` (138 cases). Degradable errors return
    no-data, count and warn; normal values pass through; Auth / NotFound /
    ValueError / TypeError / RuntimeError propagate; Protocol conformance;
    the builder wraps every provider except M23's; with degradation the
    pipeline survives an HTTP 503; with the builder default it still
    propagates.
  - `test_live_worker_wiring.py`: the worker hands Pipeline the degrading
    stages and the replay-safe store.
- 5.0.6: `test_ohlc_row_order.py` (15). Newest regular close (762.04 on the
  probed SPY rows); hand-computed realized vol; pr/po rows ignored;
  shuffle-invariant; guards.
- 5.0.9: `test_webapp_auth.py` (236), parametrized over `app.routes`.
  - Missing, wrong or malformed credentials → 401 plus `WWW-Authenticate`.
  - Unset or empty variables → 503.
  - `/health` is open.
  - Unknown paths are gated; credentials never reach the logs; lifespan
    still starts.
- 5.0.11: `test_v6_thetadata_confluence_profile.py` (7). The whole profile
  equals `v5_gamma_squeeze` except `m21.short_gamma_threshold == 0`.

No scoring module changed, so there are no score-branch truth tables.

## 4. D10 record (existing tests removed or changed)

- **Merge (5.0.3)**, full list with replacements in the commit body:
  - 16 phase-3-only UW provider tests, superseded by the 3.9 tests;
  - 2 deletions from `main` kept (`e9df314`, `b2caae9`);
  - 3 deletions from `phase-3` kept (`012bde2` ×2, `42f7672`);
  - `test_cli_run_4cell` union asserts;
  - `test_parquet_exit_quote.py` import path.
- **5.0.6** `test_gamma_live_earnings.py`: two realized-vol fixtures had rows
  without `date` and `market_time`, which the required regular-session filter
  drops. Those fields were added to the input rows; assertions unchanged.
- **5.0.7** `test_gamma_live_loop_wiring.py`: the fake `Credentials` gained
  `require_unusual_whales_api_key`, the accessor the code now calls.
- **5.0.9** `test_webapp_routes.py`, `test_dashboard_route.py`: clients send
  credentials through `tests/unit/_webapp_auth.py`; assertions unchanged.
- **5.0.10** `test_webapp_routes.py`: the expected label "vol-selling
  candidate" became "IV rich vs its 1y range", as the contract permits.

## 5. Judgment calls and contract deviations

1. **Merge test count.** Contract §3.3/§5 says 17 phase-3 UW provider tests
   leave the spec; the computed list is 16. The 17th
   (`test_open_interest_at_and_next_day_use_separate_caches`) had already
   been deleted on `main` in `e9df314`.
2. **Label wording.** Contract §3.12's example labels ("IV rich/cheap vs
   realized") describe the signal wrongly: it is IV rank within the name's own
   1-year range plus the gamma regime. Shipped labels:
   - "IV rich vs its 1y range (long gamma)"
   - "IV cheap vs its 1y range (short gamma)"
   - "IV rank not extreme for this regime"
3. **Incomplete D10 list.** Contract §5 omitted the two fixture/fake changes
   in §4 (5.0.6, 5.0.7). Both follow directly from §3.9 and the
   `require_unusual_whales_api_key` instruction.
4. **Degrading wrappers (5.0.5).**
   - They catch `UnusualWhalesRateLimitError` (which includes the
     daily-limit subclass), `UnusualWhalesTransientError` and
     `CircuitBreakerOpenError`.
   - They return `None` or `()` to match the NoOp providers.
   - The WARNING leaves out the exception message, because it can carry a
     response-body excerpt.
   - Every Protocol method is wrapped, including ones no stage calls.
5. **ohlc helper (5.0.6).** `webapp/ohlc.py` keeps only dated rows with
   `market_time == "r"`, sorted by date. `latest_close` returns None when the
   newest regular close is non-numeric, rather than using an older day. The
   current day's "r" row can be intraday while the session is open.
6. **Auth shape (5.0.9).**
   - It is ASGI middleware, not a FastAPI dependency, so `/docs`, `/redoc`,
     `/openapi.json`, unknown paths and any future mount are gated too.
   - Only `GET /health` is open; `HEAD /health` is gated.
   - Websocket upgrades are closed with 1008 unless authorised.
7. **Copy beyond the contract list (5.0.10).** Also rewritten: "walls are
   price magnets" and "often pinned" (pinning was rejected), "squeeze-prone",
   "prize axis/pattern", "informed execution", "worth watching", the journal
   "Possible edge… before sizing up", and "Size for a vol spike". The internal
   `sell`/`buy` keys stay as ordering and colour identifiers, documented as
   such.
8. **Stale code text folded into 5.0.12** (comments, docstrings and one log
   message only):
   - the 15k/day quota wording;
   - the flow_poll docstring naming `flow-recent`;
   - the fusion `_build_canonical` docstring claiming it raises on missing
     IV/OI;
   - the rest_flow drop log attributing the IV/OI requirement to fusion.
9. **Docs (5.0.12–5.0.13).** DATA_INTEGRATION gained §8–§11. CLAUDE.md gained
   the D2 `--first-parent` bisect rule (from contract §3.1). The
   paper-trading rule now reads "nothing may send, route or place an order,
   live or paper account", so it cannot be read as banning backtest
   simulation.
10. **Parallel development.** 5.0.4–5.0.5, 5.0.6–5.0.11 and 5.0.12–5.0.13 were
    built on three branches cut from `6d1e4ca`, then cherry-picked in contract
    order. §2 gates the resulting series commit by commit.
11. **CI environment (5.0.15).** The first CI run on PR #8 failed 12 CLI
    tests that pass locally.
    - Cause: Typer 0.25 forces a Rich terminal when `GITHUB_ACTIONS` is set,
      so ANSI styling and 80-column panels split the option names the tests
      assert on (`--live-tickers`, `--replay-data is required`, ...).
    - Reproduced locally with `GITHUB_ACTIONS=true` (12 failed). Cleared with
      `NO_COLOR=1 TERM=dumb COLUMNS=200` (82 passed in `tests/integration`).
    - 5.0.15 sets those variables on the CI job. No test or code changed.
      The per-commit gate in §2 is the local gate; CI runs only on the PR
      head and on pushes to `main`.

## 6. Flags for Berkay

1. **Stale journal prices.** The two journal trades (logged 2026-06-25, still
   open) carry entry prices from the old `ohlc/1d` bug, about a year stale.
   They were not rewritten. Your call: close, annotate, or delete them.
2. **Order-dependent test.**
   `test_calibration_yaml_loading.py::test_warning_logged_for_commonly_tuned_missing`
   fails when only `tests/unit` is run and passes in the full suite. It
   reproduces on a clean `6d1e4ca`, so it predates 5.0. Registry candidate.
3. **Stale non-claim copy.**
   - The `gamma.html` footer still says gamma comes from a daily ThetaData
     snapshot job; it is live UW now.
   - `vol_read` still says "IV high" for rows below the threshold.
   - Neither is an edge claim. Registry candidate.
4. **Stale doc pointers.**
   - `snapshot_reader.py`'s docstring names `scripts/compute_chain_snapshots.py`,
     which does not exist; the real script is `download_chain_snapshots.py`.
   - `docs/next_studies.md` words the burned window differently from
     `docs/preregister_D.md` §0.
   - Both are recorded in `docs/INDEX.md`.
5. **UW budget.** `x-uw-daily-req-count` was 4,613 at 18:32Z with 10 live
   tickers, against a 30,000 limit. Degradation (5.0.5) removes the restart
   storms that would re-poll on transient errors.
6. **`/health`** only proves the process is up. DB and worker liveness checks
   are 5.1 foundation scope.
7. **Merge method.** Merge this PR with "Create a merge commit". The repo
   allows squash; squashing would erase the merge ancestry.

## 7. Remaining for "done" (contract §6)

1. PR CI green; branch protection on `main` requires the `gate` check.
2. Berkay merges the PR (merge commit).
3. Pre-switch checklist §3.14:
   - local and CI gate green on `main`;
   - `test_uw_live_endpoints.py` 10/10;
   - local auth smoke;
   - tag `phase-3-final`;
   - `pg_dump` (local client 18.4, server 18.6);
   - `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` set on Railway.
4. After the close: Berkay switches the Railway source branch to `main`.
   Claude watches the deploy, `/health` and the logs.
5. First RTH session soak on `main`. Then lock `phase-3`.

## 8. Sıradaki

Phase 5.1 Foundation acceptance doc (`docs/INDEX.md` §7): login for two users,
Alembic for webapp tables with a prod stamp, UW budget governor, advice-language
lint, disclaimer, deeper `/health`.
