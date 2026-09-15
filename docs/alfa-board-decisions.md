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
