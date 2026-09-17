# Alfa Board — decision log

Living document (not a contract). Berkay's owner decision K4: when something is
ambiguous, pick the most honest and most reversible option, record it here as
**decision / rationale / how to undo**, and keep going. The binding contract is
`docs/phase-5.2-alfa-board-acceptance.md` (plus
`docs/phase-5.2-decision-cards-acceptance.md` for FAZ C).

---

## P1. Phase number and commit labels

- **Decision.** The Alfa Board is Phase 5.2, the "terminal main screen" slot in
  `docs/INDEX.md` §7, and it runs before 5.1 Foundation. Commits are labelled
  `Phase 5.2.<item>` (e.g. `Phase 5.2.A1`), mirroring the owner's item list.
- **Rationale.** Berkay ordered this program directly and in this order. The
  registry allocates numbers; it does not force build order. Item labels make
  every commit traceable to his spec line.
- **Undo.** Renumbering is cosmetic: edit the INDEX registry row. Commit
  history stays.

## P2. One PR per FAZ; merging to `main` is the deploy

- **Decision.** Branch `phase-5.2-alfa-board` is cut from `main` (`d72e30e`).
  Each FAZ (A, B, C, D) ends with a PR. Once CI `gate` is green, Claude merges
  it with a merge commit; Railway auto-deploys `main`. Claude then verifies the
  live URL and rolls back (revert PR) if it is broken.
- **Rationale.** Berkay's spec says "Faz bitince deploy et ve canlı URL'in
  çalıştığını doğrula". That is explicit approval to merge each green FAZ.
  CLAUDE.md forbids merging to `main` only *without* Berkay's approval.
- **Undo.** `git revert -m 1 <merge>` on `main` (CI plus auto-deploy), or
  Railway → redeploy the previous deployment.

## P3. Gamma board data staleness (found during the 5.2 soak check)

- **Finding.** On 2026-09-15 at 15:30Z (RTH), `gamma_regime` held 10 rows, all
  with `as_of = 2026-09-09`. Rows are rewritten after each startup reset, so
  the loop runs; the stale date comes from how UW rows are selected. The cause
  is traced in the B5 section.
- **Decision.** Recorded here. B5 (regime band) reads dealer exposure through
  the Phase 3.9 `UnusualWhalesDealerGammaProvider` (dated `spot-exposures`,
  USD per 1%) rather than `gamma_live`'s undated call. The `gamma_live` fix is
  its own commit, with a regression test.
- **Root cause** (confirmed by a live probe the same day). `GET /api/stock/{t}/iv-rank`
  lists sessions OLDEST first, and `webapp/gamma_live.fetch_one` used `data[0]`.
  The vol board's spot, IV rank and `as_of` were six days stale.
- **Fix.** Hotfix `Phase 5.2.0` (`d17bffa`) selects the newest row by ISO date.
  It shipped as its own PR ahead of FAZ A because it was live wrong data.
- **Undo.** Revert that commit.

## P4. Ruff and Turkish letters

- **Decision.** `pyproject.toml` `[tool.ruff.lint]` gains
  `allowed-confusables = ["ı", "İ"]`.
- **Rationale.** The board UI is Turkish (K3). RUF001 flags the dotless `ı` in
  every string as "ambiguous". The project already ignores RUF002/RUF003 for
  spec typography. This allows only the two Turkish I letters; every other
  confusable is still flagged.
- **Undo.** Remove the line. Every Turkish string with `ı` then needs a
  `# noqa: RUF001`.

## P5. `.env.save`

- **Finding.** No `.env.save` exists in the repo root (checked 2026-09-15 15:22Z).
  `.gitignore` already covers `.env`, `.env.save` and `.env.*` (keeping
  `!.env.example`, which is the only tracked env-like file).
- **Decision.** Nothing to delete. Rule satisfied.
- **Verified live** (PR #9 merge `4f0922a`, deployed 15:37:53Z mid-session):
  - `/health` 200; auth wall 401 without and 200 with credentials; `/gamma` and `/journal` 200.
  - Today's live run kept ingesting across the redeploy: 519 → 527 rows.
  - After one refresh cycle, `gamma_regime` held 10 rows, all `as_of = 2026-09-15`.
  - 0 error lines in the deployment log.

## P6. Board settings live in their own profile file

- **Decision.** New cutoffs go in `profiles/board_v1.yaml`, loaded into a
  strict `BoardSettings` model. They are not a new section of
  `CalibrationProfile`.
- **Rationale.** `CalibrationProfile.content_hash()` hashes the full model.
  Any new section changes the hash of `v5_default`, of the burned
  `v5_gamma_squeeze` and of the burned `v6` profile. Those hashes are stored on
  every `backtest_run` and `signal` row and trace the recorded verdicts. A
  separate file keeps D8 ("numbers live in the profile") without touching them.
  The İŞLENMEZ cutoff still *reads* `penalty_triggers.spread_pct_threshold`, as
  the owner specified.
- **Undo.** Move the `BoardSettings` fields into a `board:` section of
  `CalibrationProfile` with `default_factory`, and accept the hash change.

## P7. Stage telemetry in a side table, not on `StoredSignal`

- **Decision.** `alfa_stage_telemetry` and `alfa_print_meta` are filled by a
  `DecisionRecordWriter` on the live worker.
- **Rationale.** `StoredSignal` is `extra="forbid"` on `main` and on
  `phase-3-final`. Even a new key with a null value fails validation, so after
  a rollback the dashboard would silently drop every new row. A side table
  keeps rollback data-safe and changes no frozen stage.
- **Undo.** Drop both tables and remove the writer from `webapp/worker.py`.

## P8. A fifth display state: `nötr`

- **Decision.** A family that was measured but points neither way renders as
  `nötr (ölçüldü, yön göstermiyor)` and enters no count.
- **Rationale.** The owner's four states have no slot for a measured neutral
  result (M23 flat price, M25 all-neutral peers, M26 unclear direction).
  Folding it into `bilinmiyor` would recreate the "unknown vs measured"
  conflation this phase exists to remove. Folding it into `aleyhte` would
  inflate the counter-evidence.
- **Undo.** In `webapp/board/evidence.py`, map `nötr` to `bilinmiyor`; the
  mapping table is one dictionary.

## P9. Dealer gamma is never `aleyhte`

- **Decision.** M21's `full_short_and_proximate` maps to `lehte`, labelled
  `hareketi büyütebilir`. Every other measured M21 branch maps to `nötr`.
- **Rationale.** M21's score ignores option type (it is non-directional), and
  no pipeline code sets `gamma_flag`. There is no honest basis for "dealer
  gamma is against this call".
- **Undo.** Edit the M21 row of the mapping dictionary.

## P10. Açık pozisyon comes from T+1 confirmation only

- **Decision.** The Açık pozisyon family comes only from the next-day
  open-interest change of the dominant contract (B4). M27's print-time score
  is shown in the audit block only, labelled
  `önceki seans OI değişimi (bu baskı değil)`.
- **Rationale.** UW open interest is start-of-day, so M27 at signal time
  compares the rows for D-1 and D. That is the previous session's change, not
  the flagged print (probe 2026-09-15).
- **Undo.** Add M27 branches to that family's mapping.

## P11. Direction takes the aggressor side into account

- **Decision.** A row's direction comes from `fill_side`, recorded in
  `alfa_print_meta`: a bought call or sold put is `yukarı`, a sold call or
  bought put is `aşağı`. With unknown or mid side, or on legacy rows, it falls
  back to option type and carries a visible marker.
- **Rationale.** Today's dashboard labels every call BULLISH, including sold
  calls. `flow_poll` already computes `fill_side`, but it was never stored.
- **Undo.** Make `webapp/board/direction.py` return the option-type direction
  unconditionally.

## P12. The board is built on `/alfa`, then switched to `/`

- **Decision.** A1–A6 build on `/alfa`. Commit A7 makes `/` the board and
  removes the per-print cards. The D10 test changes are listed in that
  commit's body.
- **Rationale.** Every A-commit stays bisectable and green. The old dashboard
  and its tests stay intact until one documented switch.
- **Undo.** Revert A7.

## P13. Board-side catalyst reader; the scoring provider is untouched

- **Decision.** The catalyst chip uses its own reader:
  - FOMC and other macro events are matched by name from
    `catalyst.macro_event_names`;
  - the FDA query sends `target_date_min`.
  The Phase 3.9 provider that feeds M22/M24 is unchanged.
- **Rationale.** The probe found FOMC typed `report`, which the provider
  filters out, and FDA results truncated oldest first. Fixing the provider
  changes scoring inputs, which is Berkay's decision (registry). The audit
  block discloses that the chip and M22's score can disagree.
- **Undo.** Once Berkay approves the provider fix, point the chip at the
  provider's `catalysts_in_window`.

## P14. Substitute endpoints where the named ones are unusable

- **Decision.** Substitutes are used in these cases:

  | Named in the spec | Result | Substitute |
  |---|---|---|
  | congress `unusual-trades` | 422, premium endpoint | `/api/congress/recent-trades?ticker=` |
  | `/api/insider/{t}` | roster only, no trades | `/api/insider/transactions` |
  | `/api/shorts/{t}/interest-float` | deprecated; data ends 2021 with impossible values | `/interest-float/v2` |
  | `/api/volatility/vix-term-structure` | 403, needs the volatility add-on | chip `VIX vade yapısı: kapsam-dışı (volatilite eklentisi yok)`, plus `SPY IV vadesi` and a derived VIX spot |
  | `/api/stock/{t}/option-chains` for NBBO | only returns NBBO with `greeks=true`, as a 1.3 MB payload per ticker, with no quote time | `/api/stock/{t}/option-contracts?option_symbol[]=` |

- **Rationale.** The owner's rule: if something is impossible, write the
  evidence and move on; never fake data. Probe samples are in the session
  scratchpad (`alfa_probe/`); the HTTP statuses are quoted in the contract.
- **Undo.** Change the endpoint constant in the fetch module once access
  exists (e.g. the premium congress endpoint).

## P15. Disclosed defaults for capital, R and commission

- **Decision.** Pinned values: capital 10,000 USD, R 100 USD, commission
  0.65 USD per contract. While `sizing.values_confirmed_by_owner: false`,
  every affected cell shows `(varsayılan değer)`.
- **Rationale.** Berkay has not stated these values. 10,000 is his stated
  upper bound and 0.65 is the cost model of `docs/edge_to_money.md`. A visible
  marker is more honest than hiding the cells.
- **Undo.** Edit `profiles/board_v1.yaml` and set
  `values_confirmed_by_owner: true`.

## P16. Quote age means our fetch time plus the last print

- **Decision.** The chip shows two times:
  - `kotasyon {n} sn önce alındı`: our `fetched_at` from `option-contracts`;
  - `son işlem {m} dk önce`: `last_tape_time`, or the `/flow` quote time for
    the top-K rows, labelled `son işlem anında`.
- **Rationale.** No REST endpoint returns a live order-book timestamp. The
  `/flow` quote is frozen at the contract's last print (the probe saw a SMCI
  quote 4 minutes old). Calling either time "live" would be false.
- **Undo.** Not applicable until a live-book source exists (UW websocket,
  ThetaData).

## P17. One long-lived UW client for the board, with a soft daily cap

- **Decision.** The board refresher owns a single client for its lifetime.
  `UnusualWhalesClient` gains a read-only attribute holding the last seen
  `x-uw-daily-req-count`. Non-critical fetches pause at
  `refresh.daily_request_soft_cap`.
- **Rationale.** The worker and gamma loop already build separate clients.
  Response headers were invisible, so budget exhaustion showed up only as a
  daily-limit 429 that starves the live flow poller.
- **Undo.** Remove the attribute read. Client behaviour is otherwise
  unchanged.

## P18. Contract §3 vs §4.3: where board cutoffs live

- **Finding** (reported by the FAZ A, B and D build agents). The frozen
  contract contradicts itself.
  - §3 says every new numeric cutoff goes into "a new `board:` profile section
    with an explicit value in `profiles/v5_default.yaml`". That line was
    written before decision P6.
  - §4.3 and P6 put them in the separate `profiles/board_v1.yaml`.
- **Decision.** §4.3 and P6 govern. All code follows them.
- **Rationale.** P6 protects the calibration content hashes of the burned
  profiles. §4.3 is the detailed architecture section, and §3's line predates
  the decision.
- **Change.** Per D1 the contract is not edited; this entry records the
  resolution.
- **Undo.** See P6.

## P19. Additive deviations made during the FAZ A/B/D builds

These change no rule or behaviour the owner specified. Each is reversible.

- **`alfa_atm_expiry`** (not in §4.2). Stores the once-a-day expiry list so the
  per-cycle ATM job survives a refresher restart without re-fetching.
  Rebuildable.
- **`alfa_catalyst_fetch`** (not in §4.2). Records fetch coverage per source
  and ticker. A source that was never fetched, or failed, then reads
  `bilinmiyor` instead of "no catalyst" (R-UN1). Rebuildable.
- **`alfa_catalyst` key.** Uses a string `when_key` (ISO instant, ISO date, or
  a vague label such as `2026-Q3`) instead of a timestamp, because FDA Q/H/MID
  targets have no precise time. Precise `starts_at`/`ends_at` are stored
  alongside.
- **Degraded flag storage.** Per stage on `alfa_stage_telemetry`; there is no
  column on `alfa_print_meta`. The print-level flag is the any() over its
  stages, so no information is lost.
- **Table creation.** Each module creates its own tables with
  `Model.__table__.create(checkfirst=True)` in an `ensure_*_tables` function,
  still on `AlfaBase`. This avoids a central registry file shared by parallel
  builds.
- **Live integration test files.** Split per domain instead of one
  `test_alfa_live_endpoints.py`: `test_alfa_live_quotes.py`,
  `test_alfa_live_tape.py`, `test_alfa_live_b.py`,
  `test_alfa_live_delayed.py`.
- **Daily greek-exposure staleness.** A once-a-day source cannot use the 900 s
  intraday staleness rule, which would blank it all day. It goes stale after
  one trading day.
- **Lot % of capital.** Shown in percent units, per §4.3's unit rule.

## P20. Judgment calls from the FAZ A build and review (folded in from commit bodies)

### Board profile keys added

Each key was genuinely missing and is commented in `profiles/board_v1.yaml`.

| Key | Value | Why it was needed |
|---|---|---|
| `tape.max_age_seconds` | 900 | A3 reads a stale tape as `bilinmiyor` but pinned no age |
| `narrative.counter_min_unknown_families` | 2 | AMA priority 5 |
| `refresh.max_symbols_per_request` | 200 | the live-verified `option_symbol[]` bound |

**Undo:** remove the key, and the code path that reads it.

### Evidence

- **Legacy prints.** A print with no telemetry rows is legacy.
  - A print with telemetry but missing one stage reads that family
    `bilinmiyor`.
  - An unmapped branch reads `bilinmiyor`.
  - Missing telemetry never reads `kapsam-dışı`.
- **Orientation flip.** Only M23, M25 and M26 flip for sold options. M21 never
  flips (P9). Akış and Açık pozisyon are read in the row's own direction.
- **Aggressor side.** `above_ask` counts as bought and `below_bid` as sold,
  the same as at_ask and at_bid.
- **R-UN2 guard.** It never raises. It downgrades `Güçlü` to `Orta` or `Zayıf`
  and logs ERROR, both when evidence is built and again before rendering.
- **Akış after the close.** Once the tape is older than 900 s, Akış reads
  `bilinmiyor` on evening views. Registry: a "complete session" rule with its
  own key.

### Cost and quotes

- **Crossed or zero-ask quote** (ask < bid, or ask = 0). It reads
  `kotasyon yok`, and no cost cells are rendered from it (fix1).
- **Soft cap.** "Critical" means quotes for open journal legs only. A daily
  count is honoured only on the UTC day it was observed.

### Clean candidates and banners

- **Clean candidate:** tradability `İŞLENİR`, L ≥ 1, A = 0, U ≤ 2. Dealer gamma
  alone (non-directional) cannot make a row clean (fix4).
- **The `Bugün temiz aday yok` banner** shows for any successfully read run
  with zero clean candidates. Runs that are not today's carry their date. The
  banner never shows for load-failed or no-runs states (fix4).

### Runtime

- **Startup.** The app refuses to start when the board profile is missing or
  invalid (fix3). On Railway the healthcheck then keeps the previous
  deployment serving.
- **Refresher.**
  - DB writes are set-based and run off the event loop.
  - Each step is isolated, so one failing step does not skip the others.
  - The run is chosen by newest print on today's ET date, not by
    `live-<UTC today>` (fix2). This is needed because the worker does not roll
    its run id over at UTC midnight.
  - Key failures (401/403, or no status) log ERROR and wait one cadence
    instead of looping.
- **Telemetry write failures** are counted and logged; they never raise into
  the live worker (fix7).
- **Removed with the old dashboard (A7):** its 30 s reload and the page-level
  LIVE/STALE badge. The board reloads every `refresh.cadence_seconds`.
  Per-row quote ages remain.
- **Board freshness line (fix8).** It replaces the badge with a plain dated
  sentence: `Son baskı HH:MM ET (YYYY-MM-DD) · N dk önce`. It is not a
  LIVE/STALE verdict, because a quiet ticker set can go minutes without a
  print and that is not staleness.
  - **Undo:** remove the `data-freshness` span and `AlfaPage.freshness`.

### Registry (deferred, each with a reason in the fix commit bodies)

1. FA-07: `kotasyon yok` is not a counter-argument. The owner pinned the
   priority list.
2. FA-10: the journal pages still show the legacy `signal_score`. FAZ C
   decision cards replace that entry flow.
3. R-A-1: the vol board's `IV-rank 75` literal should render from
   `rich_threshold`.
4. RT-2: `webapp/worker.py` never rolls its live run id over at UTC midnight
   without a restart. This predates FAZ A and sits on the ingestion path.
5. RT-5: the refresher is gated on the 13:30–21:00 UTC union, not the real ET
   session. The post-close window is an owner call. Spend is about 2,750–2,880
   requests/day.
6. RT-6: a live print with no telemetry is labelled "legacy" instead of
   "telemetry write failed". Fixing that needs a marker column, i.e. a schema
   decision.
7. FA-09: the gamma snapshot has no fetch timestamp. The gamma loop is a
   non-goal.
8. `notability.py`, `SignalRepo.signals`, `SignalFilters` and
   `ticker_label_options` no longer have a route caller. Their tests still
   pass. Cleanup item.
## P21. The delayed families need a fetch-coverage table

`alfa_delayed` is append-only, so a family whose source answered with nothing
stores no row — indistinguishable from a family that was never asked. R-UN1
requires the first to read `kayıt yok` and the second `bilinmiyor`.

**Decision.** A new rebuildable table `alfa_delayed_fetch` (ticker, family)
records the attempt itself, following the `alfa_catalyst_fetch` precedent
(P19). A degraded attempt never moves `last_success_at`, and truncation stays
disclosed across a degraded cycle. It lives in its own module
(`webapp/board/delayed_coverage.py`) so the no-rewrite guard on
`webapp.board.delayed` keeps holding.

**Undo:** delete the module and its two call sites; every family then falls
back to `bilinmiyor`, which is the safe direction.

## P22. The daily jobs need a clock, and the clock needs a memory

The refresher slept outside regular hours, so pre-market and post-close jobs
could never run.

**Decision.** One ordered registry of daily jobs with ET times from the
profile, trading-day aware, each with a persisted per-day marker in a new
rebuildable table `alfa_job_run`. A Railway restart therefore neither repeats
nor skips a day: a job due but unmarked runs on the next tick (catch-up is
intended), and the loop wakes for the next due job instead of sleeping past it.
Failures are isolated, retried at most once per cadence, and the daily-limit
and auth rules are the ones FAZ A already uses.

**Undo:** drop the registry entry for a job to stop it; drop `alfa_job_run` to
forget the markers (they are rebuildable).

## P23. Integration keeps both sides, always

FAZ B and FAZ D were built in parallel against the same three files.

**Decision.** Every conflict was resolved additively: both sides' fields,
sources and helpers survive, new fields are appended with defaults, and nothing
was dropped to make a merge easier. The one non-mechanical merge was the
TYPE_CHECKING import block, where FAZ B's block is a superset plus
`DelayedSettings`.

**Undo:** revert the merge commit; both branches still exist.

## P24. The render budget test measured the machine, not the code

The contract's "p95 ≤ 1.5 s at 2,000 signals" failed on CI at 2.63 s and on a
loaded laptop, while the same tree measures about 0.5 s on an idle one.

**Decision.** The test now asserts what is hardware-independent: ten times the
signals may not cost more than twice ten times the time (an N+1 or quadratic
regression fails on any machine), plus a generous absolute ceiling. The
contract's number is verified where it is meaningful —
`scripts/verify_live_board.sh` measures the live render time after every
deploy and reports it.

**Registry REG-4:** FAZ B and FAZ D did make the 2,000-signal render slower.
Production runs are about 1,000 prints and 20 rows, so this is not urgent, but
it should be revisited before the board is pointed at runs of that size.

**Undo:** restore the absolute assertion in
`tests/unit/test_alfa_route.py::test_alfa_render_budget_p95_at_2000_signals`.

## P25. /health says which build answered

**Decision.** `/health` returns `{"ok": true, "sha": "<7 chars>"}` from
`RAILWAY_GIT_COMMIT_SHA` (a public commit id, never a secret), so an autonomous
deploy check can prove which build is live instead of trusting timing.
`scripts/verify_live_board.sh` compares it with the expected sha, and
`scripts/audit_board_html.py` runs the §2 honesty rules against the live HTML,
failing as VACUOUS if it audited zero rows.

**D10.** `test_health_is_open_in_every_case` pinned the exact payload in 18
parametrisations; it now asserts `ok is True` and that a sha is present. Its
subject — /health stays open under every auth configuration — is unchanged.

**Undo:** return `{"ok": True}` and drop the sha check from the rail.

## P26. Decision cards were built before FAZ B was merged

FAZ C1 is the only irreversible item in the program: a pass that is not
recorded today is lost forever, while every UI polish can be redone tomorrow.

**Decision.** C1 was started on the integration tip (FAZ A + FAZ D + rails)
without waiting for FAZ B, and freezes the row view with a generic recursive
serializer, so FAZ B's fields join the snapshot automatically once merged.

**Undo:** revert the C1 commits; no other feature depends on them.

## P27. One red commit in the middle of the FAZ B+D branch

`Phase 5.2.B-merge` fails the old render-budget test in isolation on a loaded
machine; the fix is the next commit (`B-fix8`). Rewriting the history of a
branch with an open PR would have meant a force-push that cancels the running
CI.

**Decision.** The history is left as it is and the fact is recorded here, so a
future `git bisect` across that commit knows the failure is the
machine-dependent threshold, not the board.

**Undo:** nothing to undo; the branch tip and every later commit are green.
## P28. The contract's render budget is measured in production, not in a test

The contract asks for p95 ≤ 1.5 s at 2,000 signals. That number was only ever
checked on a developer machine, where the board rendered in 0.6 s — while the
live board took **3.9 s**, and 9.4 s with FAZ B and FAZ D. Nothing noticed,
because nothing measured the live render.

**Decision.** The unit test asserts what is hardware-independent (ten times the
signals may not cost more than twice ten times the time, plus a generous
ceiling), and the contract's number is measured where it means something:
`scripts/verify_live_board.sh` times the live render after every deploy and
prints it next to the honesty audit and the `alfa_*` row counts.

**Undo:** restore the absolute assertion in
`tests/unit/test_alfa_route.py::test_alfa_render_budget_p95_at_2000_signals`
and drop the timing line from the rail.

## P29. A slow page is a broken page, and the rule was applied to my own work

FAZ B+D deployed green by every check that existed at the time — 200s
everywhere, honesty audit passing, tables created, clean logs — and rendered in
9.4 s.

**Decision.** It was reverted within the owner's 15-minute rule
(`7728ba0`), measured, fixed, and brought back only once the live number was
proven: the board now renders in 0.40 s, and the largest run in the database
(6,300 prints) in 1.03 s.

**What it cost:** about two hours and three extra deploys. **What it bought:**
the board was never left slow for the owner, and the cause turned out to be one
line that would otherwise have been buried under FAZ B/D's new row content.

**Undo:** `git revert -m 1 <the re-land merge>`; the FAZ B/D work also still
exists on `p52-faz-b`, `p52-faz-d` and `p52-int`.

## P30. One builder makes the card and the page, so a card cannot lie

FAZ C1 landed while FAZ B+D was reverted, so its page builder had to drop the
delayed-evidence source that did not exist on main. Bringing FAZ B+D back
restored it — and moved **every** FAZ B/D data source (ATM, flow-since,
open-interest confirmation, catalysts, regime, journal trades, ETF holdings,
sectors, delayed) from the route into `_build_board_page`.

**Why it matters.** `GET /` renders that builder's output and `POST /alfa/card`
freezes the very same object. Sources wired in the route would have been in the
page but not in the card, so a decision card could have described a row the
owner never saw. With one builder the card freezes size, break-even, chase,
opening/closing, the catalyst chip, the regime band, portfolio overlap and the
delayed bucket automatically — the snapshot serializer is generic and needs no
field list.

**Undo:** move the source arguments back to the route; the card then silently
stops recording them, which is the failure this prevents.

## P31. Temporary instrumentation that produced nothing is not left behind

`Phase 5.2.PERF2` added one INFO line per render with the per-stage timings, to
aim the next fix by measurement. The fix landed before those lines were needed,
and they never appeared in the Railway logs — the app logger's INFO output does
not reach the deployment log.

**Decision.** The instrumentation stays only until the closeout commit removes
it, and the logging gap is recorded as **REG-6**: application `INFO` logs are
invisible in production, so anything worth seeing there must be raised to
`WARNING` or emitted through the existing structured logger. Anyone debugging
production later needs to know that before trusting a quiet log.

**Undo:** nothing to undo once removed; to re-enable, re-add the timing block
and raise its level.

## P32. A job that no registry holds never runs, and the test that covered it asserted its own construction

FAZ C2's daily outcome job — the counterfactual scoring that gives the pass
ledger its entire point — was written, reviewed, tested and shipped, and then
never ran. `build_registry` returned six jobs and never appended it. Production
proved it on 2026-09-17: `alfa_job_run` held `oi_confirm`, `catalysts`,
`gamma_history`, `etf_holdings`, `daily_close` and `delayed` for two dates, and
no `outcomes` marker for any date. `alfa_outcome` did not exist in the database.

The test named `test_faz_c_appends_its_outcome_job_with_one_entry` built the
extension itself — `extended = (*build_registry(settings), outcomes)` — and then
asserted that `extended` ended with the outcome job. It asserted its own
construction, so it could not fail, and 4,000 green tests said nothing about
whether the job was wired.

**Decision.** `build_registry` appends the entry through `outcome_jobs()`, so the
job's time and body have one owner; `_as_daily_job` is where `JobContext` is
checked against the job's Protocol under `mypy --strict`, keeping
`outcome_job.py` free of an import cycle; the refresher creates `alfa_outcome`
alongside the other daily-job tables. The test now asserts what the refresher
receives.

**The general rule this earns:** a test whose subject is *wiring* must read the
value the production caller reads. If the test constructs the composition it is
checking, it is testing the test.

**Undo:** drop the appended entry from `build_registry`; the job stops running
and nothing else changes — `alfa_outcome` is append-only and survives.

## P33. The live auditor failed a correct page for the second time

`scripts/audit_board_html.py` decides whether a deploy is trusted, so a false
positive in it costs what a regression costs. On 2026-09-16 it read DAR rows as
unquoted; on 2026-09-17 it demanded the literal `AMA` on every row and failed
four rows that rendered R-CA1's other legal shape — the contract's
`Bariz bir karşı argüman bulunamadı — bu bir onay değildir` followed by
`Bakılanlar: ...`, which is exactly what the contract requires when nothing
counts against a row.

**Decision.** The check accepts either shape and still fails a row that carries
neither, and still fails the fallback without its checked list.
`tests/unit/test_audit_script.py` pins both directions and the vacuity guard.
The strings are read from the frozen copy, never spelled out in the script.

**Undo:** restore the single `"AMA" in row` test; the auditor then fails honest
pages whenever a row has no counter-evidence.

## P34. The gamma reset is load-bearing, so it stays until someone decides otherwise

The registry carried **REG-1** as a small cleanup: `webapp/gamma_live.py` drops and
recreates `gamma_regime` on every startup, which leaves the vol board empty until
the first refresh after the open. Reading the code shows why it is there: it
absorbs the Phase 4.34 `next_earnings` column without a migration, because
`create_all` never adds a column to an existing table. Removing it would make the
first upsert fail against the old production table and stall the gamma refresh.

**Decision.** No code change. The honest options are a real migration or an
accepted rebuild, and that is the owner's call, not a cleanup. Recorded here so
the next session does not "tidy" it away. `gamma_regime` is not an `alfa_` table,
so the never-drop invariant is not involved either way.

**Undo:** nothing to undo; to act, pick one of the two options above.

## P35. One cutoff that lived in three places

**REG-3** described the vol board's IV-richness cutoff as a literal that should be
read from the profile. It was worse than recorded: the number existed three
times — as the default of `build_vol_board`, as the text `IV-rank ≥75` in the
summary sentence, and again as `IV-rank 75` in the template's threshold divider.
Two of the three were display. Moving the cutoff would have left the page stating
a number it had not ranked with, and nothing would have failed.

**Decision.** `webapp/vol_board.py` holds one `DEFAULT_RICH_THRESHOLD`, which both
the ranking and the sentence read, so the module stays testable without a profile.
Production reads `regime.vol_rich_iv_pct` from `profiles/board_v1.yaml` and hands
that one value to the ranking, the sentence and the divider. Three tests pin it:
the sentence follows the cutoff it ranked with, the rendered divider states the
profile value, and the profile and the module default may not disagree.

**Note, not fixed:** the vol board still carries English UI text beyond the empty
state that REG-2 fixed — the section heading, `ranked by IV-rank · context only`
and the divider sentence. Owner decision K3 makes the UI Turkish, so this is a
real gap; it is a wider change than this one and is recorded rather than smuggled
in. Two empty states are now Turkish (`_vol_board.html`, `gamma.html`).

**Undo:** restore the literals; the page then states a cutoff nothing guarantees.

## P36. The render variance was never ours: the board shares a database with a much larger application

Yesterday this was closed as "production-side variance I could not reproduce". It
is reproducible, and it was never our variance.

**Measured on production, 2026-09-17 13:10Z.** The run-list query the board runs on
every page load — `SELECT run_id, count(*), max(ts) FROM signal GROUP BY run_id` —
has an execution time of **20.774 ms** over 17,900 rows. The stage that contains it
measured **440 ms**, steady across eight renders eleven minutes after a deploy, so
it is not the cold window and it does not warm away. The plan says why:
`Buffers: shared read=3590` against `shared hit=117`. The 31 MB table is not in the
buffer cache, so every render reads it off disk.

**Why it is not in the cache.** `shared_buffers` is 128 MB; the database is 18 GB and
is shared with a large unrelated application — `registry_pedigree_edges` 7.46M rows,
`match_score_flags` 6.31M, `candidate_prediction` 4.65M, plus dozens of
`ar_*` / `horse_*` / `pedigree_*` tables. Its read volume is on another scale:
`tjk_horse_registry` alone has 1,619,956,388 disk block reads. That workload sweeps
hundreds of gigabytes through a 128 MB cache, and the board's small tables are
evicted as collateral. `signal` sits at a 63.5% cache hit ratio.

**This one mechanism explains everything that was open:**

- the same build measuring 0.014 s and 0.44 s minutes apart (cache hit vs miss);
- no local reproduction (sqlite, warm, single tenant);
- `pool_pre_ping` measuring *worse* twice — the cost is disk I/O, not connection setup,
  and pre-ping adds a round trip on every checkout;
- `/defter` staying fast throughout (tiny reads);
- the two 9–10 s episodes of 2026-09-16.

**Decision.** No code change on this commit, and PR #35 is cleared: the stages that
grew (`runs`, `page`) are ones it does not touch, `_board_settings()` is a cached
module global, and the same fingerprint is recorded as REG-7 on builds that predate
it. Reverting three correct fixes on a correlation the evidence contradicts would
have been the wrong call.

**What to do about it is the owner's, because one option costs money:**

1. Give the board its own Postgres instance. Clean and immediate; costs a second
   Railway database.
2. Keep hot reads off the shared disk: the board needs only `run_id`, `count` and
   `latest_ts`, which is a summary a tiny `alfa_` table can carry and the worker can
   maintain, instead of a 31 MB scan on every page load. Inside our control, no
   threshold and no contract change.

Recommended: do 2 regardless, and 1 as well if the board must stay fast while the
neighbour is busy.

**Undo:** nothing to undo; this entry records a measurement and two open options.
