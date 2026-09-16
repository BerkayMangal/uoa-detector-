# Documentation index

Living document, not a contract. `main` carries two lineages after the
Phase 5.0 merge (`6d1e4ca`): `main` (Phase 3.5.0–3.9) and `phase-3`
(Phase 3.3.9–3.6 and 4.1–4.43). They diverged at `b7de170`. This index
states current truth across both, types every doc, records label
collisions, lists burned data windows, and allocates Phase 5.x numbers.

Frozen docs are never edited (D1). Where a later phase superseded one, this
index records it. Created in Phase 5.0.12 per
`docs/phase-5.0-merge-acceptance.md` §3.13 ("the contract" below).

---

## 0. Canonical current truth

**Edge.** Canonical statement, verbatim from the contract §3.12:

> No tradeable edge found. Directional UOA confluence (UW-free, v6):
> REJECTED (phase-3.6-closeout). Gamma-regime directional and pinning:
> REJECTED (4.9, 4.11). Vol premium: untradeable after costs
> (edge_to_money.md). Conditioning replication: WEAK (study_D_result.md).
> UW-fed Track B (v5_gamma_squeeze with real UW enrichment) has never been
> testable, because UW history is about 7 days (phase-3.5.5-status B1).

Whether UW-fed Track B is closed or open is an open item for Berkay (§7).

| Clause | Evidence | Commits |
|---|---|---|
| Directional UOA confluence (v6): REJECTED | `docs/phase-3.6-closeout.md`; mechanical verdict `docs/phase-3.6-results.md` (`run-4cell --trades replay`; scenario 4: best cell `tier2_fusion` Sharpe 0.63, walk-forward 0.38) | `073218c`, `20d5988`, `ad2374a`, `b99a32f`, `d1bbfb7` |
| Gamma-regime directional: REJECTED | `scripts/study_gamma_regime.py` | `1d00516` (Phase 4.9) |
| Gamma pinning / reversion: REJECTED | `scripts/study_gamma_pinning.py` | `ed45ecb` (Phase 4.11) |
| Vol premium: untradeable after costs | `docs/edge_to_money.md` (`scripts/edge_validation.py`) | `a5af279` (Phase 4.22) |
| Conditioning replication: WEAK | `docs/study_D_result.md`, pre-registered in `docs/preregister_D.md` and `docs/preregister_D_addendum.md` | `2e3094a`, `6c189e4`, `ba09f92` (Phase 4.32) |
| UW-fed Track B never testable | `docs/phase-3.5.5-status.md` B1 (probed 2026-05-18: the subscription returns the last 7 trading days) | `e2f7350` |

**Unusual Whales layer.** Phase 3.9, live-verified 2026-09-14:
`docs/phase-3.9-uw-endpoint-correction-acceptance.md` and
`docs/phase-3.9-closeout.md`. phase-3's Phase 3.3.9 layer is superseded
(§5; `docs/MODULES.md`, "UW provider lineage").

**Live deployment.** The FastAPI webapp (`webapp/`) on Railway, with the live
worker and the gamma refresh loop running inside the same process
(`docs/DATA_INTEGRATION.md` §8–§10). Railway deploys `main` after the Phase
5.0 switch (contract §3.14); before the switch it deploys `phase-3`.
`phase-3` takes emergency hotfixes only from the merge commit on. Its tip is
tagged `phase-3-final` in the pre-switch checklist, and the branch is locked
after one clean session on `main`.

**Decision support only.** Nothing executes and there is no broker link.
Trade entry is always Berkay's manual decision.

**Backtest engines.** `--trades historical` (main 3.5.0, same-UTC-day exit
quote) and `--trades replay` (phase-3 3.5.5, 3-month walk-back). Their
verdicts are not comparable (contract §3.6; `docs/BACKTEST.md`, "Merged
run-4cell engines").

## 1. Living references

These are updated to match reality. Everything else under `docs/` is typed
in §2–§4.

| Path | Covers |
|---|---|
| `README.md` | what the product is, positioning, quick start |
| `CLAUDE.md` | working rules D1–D12, gate, commit flow, current state |
| `CALIBRATION.md` | tour of the calibration surface (`profiles/*.yaml`) |
| `docs/MODULES.md` | M21–M28 reference, UW provider lineage, self-derived ThetaData providers, webapp consumers |
| `docs/DATA_INTEGRATION.md` | credentials, downloads, Railway runtime, environment variables, UW budget, data scripts |
| `docs/BACKTEST.md` | store, replay harness, metrics, 4-cell runner, merged engines, verdict engine, sanity audit |
| `docs/INDEX.md` | this file |

## 2. Shared history (docs present at the fork `b7de170`)

Types used in §2–§4:
- **FROZEN**: acceptance contract (D1).
- **RECORD**: result, verdict, closeout or validation record.
- **STATUS**: point-in-time status or finding.
- **PREREG**: pre-registration frozen before a run.
- **PLAN**: runbook, template or roadmap.
- **DESIGN**: spec.
- **AUDIT**: audit report.

| Doc | Phase | Created | Type | Post-fork note |
|---|---|---|---|---|
| `docs/phase-3.2-acceptance.md` | 3.2 | `d99a258` | FROZEN | |
| `docs/phase-3.3-acceptance.md` | 3.3 | `164961f` | FROZEN | phase-3 appended 3.3.8/3.3.9 follow-up sections in `73234b8`; accepted as an addendum, endpoint facts superseded by 3.9 (contract §3.7) |
| `docs/phase-3.3.7-acceptance.md` | 3.3.7 | `b3b36f0` | FROZEN | |
| `docs/thetadata-v3-migration.md` | 3.3.7.1 | `9229cbc` | DESIGN | ThetaData v2 → v3 mapping |
| `docs/phase-3.3.7-validation.md` | 3.3.7.5 | `6ae21be` | PLAN | validation template for Berkay's local run |
| `docs/phase-3.3.8-acceptance.md` | 3.3.8 | `b7de170` | FROZEN | added in the fork commit |
| `docs/phase-3.4-acceptance.md` | 3.4 | `f0998dd`; closeout `781dc84` | FROZEN | |
| `docs/phase-3.5-acceptance.md` | 3.5 | `559e4f5` | FROZEN | phase-3 edit `2224077` (sample-size gate) ratified retroactively (contract §3.7); names the phantom results path and "Phase 5 (paper trading)" (§5) |
| `docs/phase-3.5.1-validation.md` | 3.5.1 | `ed859cc` | PLAN | diverged (§4, §5) |
| `docs/MODULES.md` | 3.4.9.4 | `a6d78b2` | living | main `6e44bcd` (3.9) kept by the merge over phase-3 `73234b8` (3.3.9) |
| `docs/DATA_INTEGRATION.md` | 3.3.6.1 | `dc6ff38` | living | main `6e44bcd` |
| `docs/BACKTEST.md` | 3.2.1 | `2921b8e` | living | |
| `README.md` | 1 | `ba9908e` | living | phase-3 `cf98712` (Phase 4.23 positioning); updated in 5.0.13 |
| `CALIBRATION.md` | 2.10 | `e626ffa` | living | |
| `CLAUDE.md` | none | `637ce30` | living | unchanged on both lineages; updated in 5.0.13 (contract §3.13) |

## 3. `phase-3` lineage (`b7de170..f0d469f`)

phase-3 history is not D2-clean. Bisect `main` with
`git bisect --first-parent` (contract §3.1).

| Doc | Phase | Key commits | Type | Superseded by / note |
|---|---|---|---|---|
| `docs/phase-3.3.9-acceptance.md` | 3.3.9 | code `76e1a16`…`1cc04c1`; doc `73234b8` | FROZEN | superseded by 3.9 (§5) |
| `docs/phase-3.5.1-validation-record.md` | 3.5.1 | `73234b8` (at `docs/phase-3.5.1-validation.md` on phase-3); copied verbatim in 5.0.12 | RECORD | KAPALI, run 2026-05-12, 81 passed / 1 skipped. The UW paths it validated were 3.3.9's, since superseded. |
| `docs/phase-3.5.2-dry-run.md` | 3.5.2 | `6f54118` | RECORD | KAPALI; per-contract `scripts/download_tier2.py`, replaced by the bulk download |
| `docs/phase-3.5.3-config.md` | 3.5.3 | `2224077`, `856ea48` | PLAN | bulk download runbook (`scripts/download_bulk.py`, 3.5.3.9 `f3944a7`) |
| `docs/phase-3.5.4-synthetic.md` | 3.5.4 | `22178e3` | RECORD | KAPALI; `run-4cell --trades synthetic` wiring smoke |
| `docs/phase-3.5.5-status.md` | 3.5.5 | `f61071e`…`93158c1` | STATUS | B1: UW history is the last 7 trading days; B0 (full UW history) never obtained |
| `docs/phase-3.5.7-audit.md` | 3.5.7 | `b3c882c` | AUDIT | PARTIAL; tooling `backtest/sanity_audit.py` |
| `docs/phase-3.6-acceptance.md` | 3.6 | `e07260e`; edits `d351eec`, `22dbf56` | FROZEN | the two edits are a recorded D1 breach; the 3.6 verdict stands (contract §3.7) |
| `docs/phase-3.6-results.md` | 3.6.3–3.6.6 | `073218c`, `20d5988`, `ad2374a` | RECORD | `--verdict-path` output of `run-4cell --trades replay`; EDGE REJECTED |
| `docs/phase-3.6-closeout.md` | 3.6 | `b99a32f` (tag `phase-3.6-complete`); addendum `d1bbfb7` | RECORD | CLOSED, EDGE REJECTED; the `b5fcd7a` look-ahead leak fix |
| `docs/phase-4-screener-design.md` | 4 | `d4c7b5a` | FROZEN | web UI design and acceptance ("frozen on commit"); "Phase 4" is phase-3's label (§5) |
| `docs/audit_report.md` | 4.21 | `6f21a09` | AUDIT | repo audit dated 2026-06-24 |
| `docs/edge_to_money.md` | 4.22 | `a5af279` | RECORD | vol premium does not survive costs, tail and multiple testing |
| `docs/next_studies.md` | 4.24 | `0939271` | PLAN | candidate map, no study started. Its "+0.77 Sharpe" conditioning premise was tested by Study D: WEAK. Its window wording disagrees with the Study D inventory (§6). |
| `docs/preregister_D.md` | 4.25 | `2e3094a` | PREREG | Study D design, frozen before any computation |
| `docs/study_D_result.md` | 4.26, 4.32 | `7e9a0f6` (BLOCKED), `ba09f92` (verdict) | RECORD | WEAK / BORDERLINE |
| `docs/preregister_D_addendum.md` | 4.27 | `6c189e4` | PREREG | causal trailing `iv_pct` is the decision metric |
| `docs/study_D_result_template.md` | 4.30 | `62ddc95` | PREREG | reporting format frozen before the run; filled into `docs/study_D_result.md` |
| `docs/vol-premium-dashboard-design.md` | 4.33 | `abe22a9` | DESIGN | its "highest-probability honest edge" framing is superseded by §0 and the 5.0.10 copy changes |
| `docs/vol-premium-dashboard-plan.md` | 4.33.1 | `aeff920` | PLAN | its gate command is superseded by the `CLAUDE.md` gate |

## 4. `main` lineage (`b7de170..27e2842`, then Phase 5.0)

| Doc | Phase | Key commits | Type | Superseded by / note |
|---|---|---|---|---|
| `docs/phase-3.5.1-validation.md` | 3.5.1 | `ed859cc` (pre-fork template); `04b9e2e` (PR #2) | PLAN | credential validation runbook; the path keeps this version (§5) |
| `docs/phase-3.5-blocker.md` | 3.5.0 | `b2caae9` (PR #3) | STATUS | finding: the verdict engine was never wired; answered by `docs/phase-3.5.0-acceptance.md` |
| `docs/phase-3.5.0-acceptance.md` | 3.5.0 | `b2caae9` (PR #3) | FROZEN | `--trades historical`, same-UTC-day exit quote (§5). Ratifying its fixture-path judgment call is an open item (§7). |
| `docs/phase-3.6-screener-acceptance.md` | 3.6 | `f2c1ac0` (PR #4) | FROZEN | CLI `screener` digest; the label collides with phase-3's 3.6 (§5) |
| `docs/phase-3.7-rest-flow-acceptance.md` | 3.7 | `b178402` (PR #5) | FROZEN | its endpoint `/api/option-flow/recent` never existed; corrected in 3.9 §3.1 |
| `docs/phase-3.8-live-enrichment-acceptance.md` | 3.8 | `2f0b8af` (PR #6) | FROZEN | the M21–M27 providers it wired returned HTTP 404 until 3.9 |
| `docs/phase-3.9-uw-endpoint-correction-acceptance.md` | 3.9 | `dd287fa` | FROZEN | current UW layer. Its §4 UW rate non-goal is superseded by contract §3.4. Its §3.1 rationale "a print without IV would abort fusion" is stale since `012bde2` (§5). |
| `docs/vendor/unusualwhales-openapi.json` | 3.9 | `dd287fa` | RECORD | vendored spec, advisory; wrong against live responses in places (3.9 contract §2) |
| `docs/phase-3.9-closeout.md` | 3.9 | `6e44bcd`; merged via PR #7 `27e2842` | RECORD | KAPALI. §7 flags go to the 5.10–5.19 registry. Flag 10(a) is fixed by the merged `22178e3`; flag 10(b) by the merged `b5fcd7a` (`backtest/simple_pnl.py`, used by both engines). |
| `docs/phase-5.0-merge-acceptance.md` | 5.0 | `153d891` | FROZEN | this unification |
| `docs/INDEX.md` | 5.0.12 | this commit | living | |

## 5. Collisions

| Topic | `main` | `phase-3` | Current truth |
|---|---|---|---|
| UW endpoint migration: 3.3.9 vs 3.9 | Phase 3.9, `dd287fa`…`112667c`, PR #7 `27e2842` | Phase 3.3.9, `76e1a16`…`73234b8`; later 4.6 (`d55c148`) and 4.18 (`45225f2`) | 3.9 won in `6d1e4ca` (contract §3.3). 3.9 ported 4.6's fill-side rule and 4.18's 429 retry (3.9 contract §3.1, §3.9). The 3.3.9 errors are listed in 3.9 contract §2 and `docs/MODULES.md`. The 16 phase-3 provider tests that left the spec are listed in the merge commit body. |
| Backtest engine: 3.5.0 vs 3.5.4 / 3.5.5.x | 3.5.0 `b2caae9`: `historical_trade_producer` and `backtest/simple_pnl.py` `ParquetExitQuoteProvider` (same UTC day) | 3.5.4 `22178e3` (synthetic producer); 3.5.5.1 `cad7d8c`: `backtest/parquet_exit_quote.py` `ParquetExitQuoteProvider` (3-month walk-back); 3.5.5.2 `2738091`: replay producer | Both engines kept, as `--trades historical` and `--trades replay`. Two classes share the name `ParquetExitQuoteProvider`. The package export `uoa_detector.backtest.ParquetExitQuoteProvider` is main's; phase-3's is imported by module path. Verdicts are not comparable (contract §3.6). |
| Phase 3.5.1 doc | runbook at `docs/phase-3.5.1-validation.md` (`04b9e2e`) | KAPALI record at the same path (`73234b8`) | The path keeps main's runbook. phase-3's record is preserved byte-identical at `docs/phase-3.5.1-validation-record.md` (blob `9dd636f`, equal to `f0d469f:docs/phase-3.5.1-validation.md`). |
| Label "Phase 3.6" | daily screener digest, `docs/phase-3.6-screener-acceptance.md` (`f2c1ac0`) | self-derived confluence: `docs/phase-3.6-acceptance.md`, `-results.md`, `-closeout.md` | Both histories stand. The annotated tag `phase-3.6-complete` points to `b99a32f`, the phase-3 closeout, not to main's screener. |
| "screener" | CLI command `uoa-detector screener` (digest 3.6, `--source rest` 3.7, enrichment 3.8, endpoints 3.9) | web UI "UOA Screener" (`docs/phase-4-screener-design.md`, Phase 4.1 `3e8cf23`) | Both exist on `main`. Say "CLI screener" or "web app". |
| `docs/phase-3.5-results.md` | named in `src/uoa_detector/backtest/falsification.py:24`, the `--verdict-path` help at `src/uoa_detector/cli.py:1213`, `docs/phase-3.5-acceptance.md:74,299,309`, and `CLAUDE.md` until 5.0.13 | same references | The file never existed. Tests use the name only as a tmp path (`tests/unit/test_falsification.py:317`, `tests/integration/test_cli_run_4cell.py:184`). The real mechanical verdict is `docs/phase-3.6-results.md`; current truth is §0. |
| Fusion on IV-less / OI-less prints | the 3.9 contract §3.1 and the `rest_flow.py` rationale said such a print aborts fusion | `012bde2` (3.5.5.3): IV and OI optional; fusion carries `None` | `012bde2` accepted (contract §3.5). `rest_flow.py` still drops such prints by design; its docstring was corrected in 5.0.12. The `SourceFusion._build_canonical` docstring in `src/uoa_detector/fusion/source.py` still says "raises"; the code does not. |
| "Phase 4" | pre-fork `CLAUDE.md`, until 5.0.13: "running live, sending alerts, paper trading, etc. Phase 4+ territory" | Phase 4.1–4.43 (`3e8cf23`…`f0d469f`): webapp, live worker, studies; `docs/phase-4-screener-design.md` | 4.x labels are phase-3 history only and are not reused (§7). |
| "Phase 5" | Phase 5.x product registry (§7) | none | `docs/phase-3.5-acceptance.md:367` ("Phase 5 (paper trading)") and `:399` ("Phase 5+") use the old sense. The frozen doc is not edited. Phase 5 now means the registry; the log-only virtual portfolio is 5.6. |

## 6. Burned data windows

A new study needs a fresh, non-overlapping window (`docs/next_studies.md`,
"Two hard rules"). Registry 5.20+ allows non-burned windows only.

| Window | Data | Status | Evidence |
|---|---|---|---|
| 2025-05-01 → 2026-04-30 | `data/historical/bulk/` (24 tickers × 12 months of ThetaData `trade_quote`) | burned: the Phase 3.5.5/3.6 replay runs; the 3.6 verdict period | `docs/phase-3.5.5-status.md`; `docs/phase-3.6-results.md` |
| 2025-05-01 → 2026-04-30 | `data/chain_snapshots/` (251 snapshot dates, 24 tickers), `data/spot_series/` | burned: the 4.x studies and `scripts/edge_validation.py` use all of it | `docs/preregister_D.md` §0; `docs/next_studies.md` rule 1 |
| 2024-05-01 → 2025-04-30 | `data/chain_snapshots_2024/`, `data/spot_series_2024/` | used by the single pre-registered Study D run (`ba09f92`); not fresh for a later study | `docs/preregister_D.md` §1; `docs/study_D_result.md` |
| 2026-05-01 onward | forward data | registered as Study D's stricter alternative; underpowered as of 2026-06-24 | `docs/preregister_D.md` §1 |

- `docs/next_studies.md` describes the burned panel as "11-month" and
  "Jul-2025–Apr-2026". The inventory in `docs/preregister_D.md` §0 records
  2025-05-01 → 2026-04-30 (251 dates). Treat the whole year as burned.
- **Universe.** All rows above use the same 24 tickers,
  `data/universes/_bulk_all.csv`: ABNB AI AMD BAC COIN CRWD DKNG GOOGL HOOD
  JNJ JPM LCID MARA META PLTR PLUG RBLX RIOT RIVN SNAP SOFI TSLA UNH XOM.
  Study D froze this set in `docs/preregister_D.md` §1. The replay 4-cell
  run splits it into `data/universes/tier1_reduced.csv` (9) and
  `tier2_top15.csv` (15).
- **UW history.** The subscription returns about the last 7 trading days
  (`docs/phase-3.5.5-status.md` B1). The IV-rank data has no history before
  2026-05-04 (`docs/phase-3.9-closeout.md` §7 item 7). No UW-fed historical
  window exists.
- **Where the data lives.** The data directories are local, not in git.
  `data/chain_snapshots/`, `data/spot_series/` and `data/historical/**/*.parquet`
  are gitignored. `data/medians_bulk.csv` and `data/earnings_calendar.csv`
  are committed and backtest-only (contract §4).

## 7. Phase 5.x registry

**Numbering rules**
1. Phase 3.x and 4.x labels are historical. They are never reused or
   extended.
2. New work is numbered 5.X. A number is allocated in the table below before
   any branch, doc or commit uses it.
3. Every phase has its own acceptance doc before code, named
   `docs/phase-5.X-<slug>-acceptance.md`. Its paket-mode report is a separate
   `docs/phase-5.X-closeout.md` (the 3.9 and 5.0 pattern).
4. Commit subjects are `Phase 5.X.N: <summary>`, with N counting from 1
   within the phase.
5. A hotfix to a live component during 5.x is a numbered sub-commit of the
   owning phase, with "hotfix" in the summary.

**Allocated** (contract §8)

| Phase | Scope |
|---|---|
| 5.0 | `main` ← `phase-3` unification (`docs/phase-5.0-merge-acceptance.md`); in progress |
| 5.1 | **Foundation:** login (two users, no signup), Alembic for webapp tables with a prod stamp (additive only), append-only table policy, UW budget governor + `/health/budget`, advice-language lint, disclaimer, deeper `/health` |
| 5.2 | **Alfa Board** (the terminal main screen; owner spec 2026-09-15, built before 5.1 by owner order): `docs/phase-5.2-alfa-board-acceptance.md`, `docs/phase-5.2-decision-cards-acceptance.md`; decisions in `docs/alfa-board-decisions.md` |
| 5.3 | Ticker detail |
| 5.4 | Live stream (SSE) |
| 5.5 | Watchlist + alerts (notify only) |
| 5.6 | Virtual portfolio / forward record (log-only paper positions, never executed) |
| 5.7 | Daily ideas: pre-registered, logged and forward-scored against a random control. Presented as unvalidated ideas, never as a recommendation. |
| 5.8 | AI assistant (read-only DB tools, no direct UW tool, per-user cost cap) |
| 5.9 | Landing, methodology, glossary, legal |
| 5.10–5.19 | Engine and data fixes, in registry order. First candidates: flow_poll → `/api/option-trades/flow-alerts`; stage-level `provider_error`; gamma_live units and client reuse; exit-quote staleness rule and engine retirement; falsification gates into the profile (D8); 3.9 §7 flags; D11 TODO removal; dedicated Postgres, retention and backups. |
| 5.20+ | Research studies (`preregister_<L>.md`, next letter E; non-burned windows only) |

Detail for the 5.10–5.19 candidates:
- **Falsification gates (D8).** `src/uoa_detector/backtest/falsification.py:79-81`
  hardcodes `_MIN_CLOSED_TRADES = 30`, `_MIN_BEST_SHARPE = 0.5` and
  `_MIN_BEST_WALK_FORWARD = 0.75`.
- **D11.** The two existing TODO markers are at
  `src/uoa_detector/calibration/resolver.py:48` and
  `src/uoa_detector/pipeline/stages/__init__.py:4`.
- **3.9 §7 flags.**
  - M21 deep-OTM flip.
  - FOMC typed `report`.
  - M23 EST/EDT clamp.
  - M25 share classes.
  - M26 notional truncation.
  - Point-in-time membership.
  - WS URL.

**Open items for Berkay** (contract §8; none blocks 5.0)
- whether the UW data licence allows showing data to a second person and
  sending it to the Anthropic API (gates the friend login and 5.8);
- the AI model and monthly cost cap;
- daily-ideas pre-registration parameters;
- alert channel and UI language;
- ratification of the 3.5.0 fixture-path judgment call;
- the status of UW-fed Track B;
- relabelling the seed run;
- Berkay's local untracked `data/historical/` manifests.
