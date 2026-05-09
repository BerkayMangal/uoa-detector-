# Phase 3.5 — First real backtest

This is the working acceptance doc for Phase 3.5. Phase 3.4 finished
the eight enrichment modules (M21-M28); Phase 3.5 runs them against
real historical data and answers a single binary question:

> **Is there an edge in the Track B Gamma Squeeze Precursor strategy
> as encoded in `v5_gamma_squeeze.yaml`?**

The answer is "yes" or "no". It is not "looks promising" or "needs
more tuning". The 4-cell falsification framework pinned in
Phase 3.2.4 defines exactly which patterns of results constitute
"edge proven" versus "edge rejected". Phase 3.5 runs the framework,
reads the report, and accepts the answer.

If the answer is "no", the project pauses while Berkay decides
whether to (a) revise the hypothesis, (b) try a different strategy
profile, or (c) end the project. The pause is not Phase 3.5's
problem — its problem is producing a trustworthy answer.

Phase 3.5 has eight sub-phases. Total estimated scope: 15-30
commits, 1000-2000 LoC, 80-150 new tests. The wall-clock time is
dominated by the historical data download (Phase 3.5.3) which
takes hours-to-days of real bandwidth.

---

## Approved decisions (locked in before implementation)

1. **The 4-cell framework defines "edge proven", not Berkay.** The
   four cells are `(tier1, single)`, `(tier1, fusion)`,
   `(tier2, single)`, `(tier2, fusion)`. Phase 3.2.4 pinned four
   scenarios that REJECT the strategy:
   - All four cells negative Sharpe → strategy fails outright.
   - tier1 cells positive but tier2 cells negative → edge is in the
     liquid universe where everyone else also competes; not the
     niche thesis.
   - single-source cells positive but fusion cells negative →
     fusion adds noise rather than signal; the multi-source argument
     collapses.
   - Best cell Sharpe < 0.5 OR walk-forward consistency < 0.75 →
     edge is too thin to survive transaction costs.
   These four scenarios are NOT revisited mid-Phase-3.5. Whatever
   the report says, we accept.

2. **Profile is `v5_gamma_squeeze.yaml` (Track B), not `v5_default`.**
   The default profile is too broad to test the specific hypothesis;
   Track B encodes the dealer-gamma + multi-source thesis. Tier-1
   anchor universe (20 tickers) and Tier-2 starter universe (51
   tickers) are pinned in `data/universes/`.

3. **Backtest period: 24 months of historical data** ending one
   trading day before Phase 3.5.4 starts. Tier-2 download covers 24
   months × 51 tickers; tier-1 (20 tickers) is a subset. Walk-
   forward windowing splits this into 4 quarters per Phase 3.2.4.1.

4. **No threshold tuning during Phase 3.5.** Profile thresholds were
   set as defaults in Phase 3.4. We run the backtest with those
   defaults. If results are weak, that's the answer for Phase 3.5;
   tuning is a separate Phase 3.6 conversation. Falsification
   discipline.

5. **PnL provider is `SimplePnLProvider` from Phase 3.2.3.** Entry
   at ask, exit at bid, slippage 0.02 per leg, holding window 5
   trading days, exit on DTE ≤ 2. These were pinned before the
   backtest existed; we don't change them now to match results.

6. **Each Phase 3.5 sub-phase is a discrete commit unit.** Reports,
   data downloads, and analysis artifacts are committed (or
   gitignored at the artifact level if too large) — the procedure
   is reproducible from git history alone.

7. **The "edge yes/no" decision is recorded in
   `docs/phase-3.5-results.md`** with the actual numbers and the
   verdict, and committed to the repo. No verbal-only decisions.

---

## Phase 3.5.1 — Local credential validation

Before any data download, confirm the keys obtained in Phase 3.3
are working end-to-end. This is a one-time setup; failure here
means hours of useless download attempts.

Scope:

- Berkay creates `.env` at repo root (already gitignored from
  Phase 3.3.1):
  ```
  UNUSUAL_WHALES_API_KEY=<uw_xxx>
  THETADATA_API_KEY=<thetadata password>
  THETADATA_USERNAME=<thetadata email>
  ```
- Berkay installs and starts Theta Terminal locally (Java app from
  thetadata.net dashboard). Confirm it logs in cleanly.
- Run all integration smoke tests with credentials present:
  ```
  uv run pytest tests/integration/ -v
  ```
  Expected: 20 skip → 20 pass.
  - 8 UW smoke tests
  - 3 ThetaData smoke tests
  - 1 live_market smoke (skipped if not market hours — that's fine)
  - 1 each: M21, M22, M23, M24, M25, M26, M27, M28 smoke
- If any test fails, stop and debug. Common failure modes:
  - Theta Terminal not running → ThetaData smokes fail at connect
  - Wrong UW tier → UW smokes fail with 403
  - Rate limit hit → adjust
    `data_sources.unusual_whales.rate_limit_requests_per_second`
    in profile
- A `docs/phase-3.5.1-validation.md` is committed showing actual
  pass/fail counts and the date. (No keys committed; just the
  evidence that they worked.)

Done when:

- 20/20 integration smoke tests pass locally
- `docs/phase-3.5.1-validation.md` committed with results
- Berkay confirms Theta Terminal is set up to start automatically
  for the download phase

---

## Phase 3.5.2 — Historical download dry-run

Before kicking off the full Tier-2 download (which costs real
bandwidth and time), validate the download script on a tiny subset
to catch any data-shape or rate-limit surprises early.

Scope:

- Run `scripts/download_tier2.py` with `--max-tasks 5` and a
  1-month range. This downloads ~5 ticker-months of data (~250 MB).
- Validate the output:
  - All 5 expected parquet files present
  - Schema matches `parquet_schema.RAWPRINT_PARQUET_SCHEMA`
  - Manifest JSON reports zero gaps
  - Trade count > 0 for each ticker
- Estimate full download cost from this subset:
  - Bytes / ticker-month → extrapolate to 51×24 = 1224 ticker-months
  - Wall-clock seconds / ticker-month → extrapolate
- Document estimates in `docs/phase-3.5.2-dry-run.md`. If projected
  full download exceeds `THETADATA_MONTHLY_BANDWIDTH_LIMIT` (TBD —
  Berkay confirms with ThetaData docs), flag for download splitting.

Done when:

- 5-ticker subset downloads cleanly
- Manifest validates
- `docs/phase-3.5.2-dry-run.md` committed with size and time
  estimates plus the bandwidth-fit decision

---

## Phase 3.5.3 — Tier-2 historical full download

This is the bandwidth-and-time phase. **Berkay runs it, agent does
not.**

Scope (Berkay's responsibilities):

- Ensure laptop has 50-200 GB free for `data/historical/`
- Ensure stable network connection (no Wi-Fi drops; ideally Ethernet)
- Start Theta Terminal, verify it stays running
- Run:
  ```
  uv run python scripts/download_tier2.py \
    --tier tier2_starter --start-date <2_years_ago> \
    --end-date <yesterday> --concurrency 4
  ```
- Estimated wall-clock: 8-48 hours depending on Theta Pro plan
  bandwidth and ThetaData server load
- Resume on failure is built-in (Phase 3.3.4); just rerun the same
  command if it stops
- After completion, verify with `--validate-only`:
  ```
  uv run python scripts/download_tier2.py --validate-only
  ```

**Agent does not run this command.** Agent's role: verify the
finished state by reading `.manifest.json` and committing the
manifest (gitignored data, checked-in manifest is the audit trail).

Done when:

- All 51 × 24 = 1224 ticker-months downloaded (or documented gaps
  for tickers with shorter listing history)
- `data/historical/.manifest.json` committed
- Berkay confirms validation pass

---

## Phase 3.5.4 — Pre-flight smoke: synthetic 4-cell run

Before running the full backtest, confirm the 4-cell runner produces
sensible output on the synthetic universe. This catches wiring bugs
before they consume real-data wall-clock.

Scope:

- Use Phase 3.2's existing synthetic source
- Run `python -m uoa_detector backtest run-4cell --profile
  v5_gamma_squeeze --synthetic`
- Validate:
  - 4 `CellRunResult` outputs (one per universe×fusion combination)
  - Each cell produced ≥ 1 trade (synthetic source doesn't have
    sparse-data issues)
  - Markdown comparison report has all 4 falsification sections
    populated
  - Walk-forward windows split correctly into 4 quarters per cell
- The synthetic numbers are NOT meaningful — purpose is wiring
  validation only.

Done when:

- Synthetic 4-cell run completes cleanly
- Markdown report renders without errors
- All falsification scenarios show numeric values (zeros are fine,
  missing values are not)

---

## Phase 3.5.5 — Real-data 4-cell backtest

The main event. Run the 4-cell combinatorial backtest against the
downloaded Tier-2 historical data using `v5_gamma_squeeze.yaml`.

Scope:

- Run:
  ```
  uv run python -m uoa_detector backtest run-4cell \
    --profile v5_gamma_squeeze \
    --replay-data data/historical/thetadata \
    --start-date <download_start> --end-date <download_end> \
    --output-dir reports/phase-3.5.5
  ```
- Wall-clock estimate: 30 minutes - 4 hours depending on signal
  density and machine speed (replay is event-time, modules call
  cached UW providers — UW endpoint hits per ticker per month limit
  the throughput)
- Output:
  - `reports/phase-3.5.5/comparison.md` — the 4-cell comparison
    report (Phase 3.2.4.3 format)
  - `reports/phase-3.5.5/cell_<name>.json` — per-cell metrics detail
  - SQLite store at `data/backtest_store.db` with all signal records
    (gitignored; per-cell run_id committed in `run_log.json`)

Done when:

- 4 cells complete without crash
- Comparison markdown renders with all sections populated
- Per-cell metrics JSON validates against schema

---

## Phase 3.5.6 — Falsification scoring

Read the comparison report. Apply the four pre-pinned falsification
scenarios. Decide: edge proven, or edge rejected.

Scope:

- Compute per-cell:
  - Sharpe ratio (already in `metrics.py`)
  - Walk-forward consistency fraction
  - Max drawdown
  - Trade count
- Apply falsification scenarios from Phase 3.2.4:
  1. All four cells Sharpe < 0 → REJECTED
  2. tier1 cells positive but tier2 cells negative → REJECTED
  3. single-source cells positive but fusion cells negative →
     REJECTED
  4. Best cell Sharpe < 0.5 OR walk-forward consistency < 0.75 →
     REJECTED
- If none of those four trigger:
  - Best cell is reported as "the edge"
  - Best cell metrics committed to `docs/phase-3.5-results.md`
  - Verdict: "edge proven"
- If any trigger:
  - Which scenario triggered, which cell metrics caused it
  - Verdict: "edge rejected"
  - Recommended next steps (without retuning the strategy on the
    same data)

Done when:

- `docs/phase-3.5-results.md` committed with verdict and numbers
- The verdict matches the four pinned scenarios mechanically (no
  judgment-call wiggle room)

---

## Phase 3.5.7 — Sanity audit

Even if the verdict is "edge proven", do not skip this. The most
common backtest failure is a bug that produces fake edge.

Scope:

- **Look-ahead leakage check:**
  - For each signal, confirm `event_ts < entry_ts < exit_ts`
  - Confirm no provider call uses data later than `event_ts`
  - Spot-check 10 random signals' provider call timestamps
- **Survivorship bias check:**
  - Compare downloaded ticker list against listing-status changes
    in Tier-2 starter universe
  - Document any ticker that was delisted mid-period
- **Slippage sanity:**
  - Compute average bid/ask spread on entry contracts
  - Compare against the 0.02 slippage assumption — if real spreads
    are 0.05+, the backtest is too optimistic
- **Trade frequency sanity:**
  - Best cell trade count vs Phase 3.2.4 prep target of 30/year × 2
    years = 60
  - If too many trades (>200), backtest may be too easy to satisfy;
    if too few (<20), statistical insignificance
- **Distribution check:**
  - Plot trade PnL distribution
  - Visual inspection: does it look like a real distribution or
    suspiciously bimodal / single-spike?

Done when:

- `docs/phase-3.5.7-audit.md` committed with checks and findings
- If any check finds a problem severe enough to invalidate
  Phase 3.5.6's verdict, that is documented and Phase 3.5's verdict
  is marked as "withdrawn pending fix"

---

## Phase 3.5.8 — Closeout

Phase 3.5 wraps up. Whether the verdict was yes or no, the closeout
is the same: document what was learned, freeze the artifact set,
prepare for Phase 4 (or for project pause).

Scope:

- `docs/phase-3.5-acceptance.md` updated with final status
- All Phase 3.5 sub-phase docs cross-linked from a single index in
  `docs/PHASE_3.5_README.md`
- Tag `phase-3.5-complete` on the relevant commit
- Phase 3.6 prep notes (if applicable):
  - If "edge proven": Phase 4 (live observer dashboard, Telegram
    alerts) and Phase 5 (paper trading) become next-up
  - If "edge rejected": Phase 3.6 becomes a hypothesis-revision
    discussion; agent does not start anything new without Berkay
    direction

Done when:

- All Phase 3.5 docs are committed and cross-linked
- Tag set
- Berkay has read the verdict and explicitly says "next step"

---

## Cross-cutting acceptance — applies to every commit in 3.5.x

- `pytest -q` green at every commit (no exceptions for "this commit
  just adds docs")
- `mypy --strict` clean
- `ruff check .` clean
- **No new module/stage code in Phase 3.5** — all modules were
  Phase 3.4. If a bug is discovered, the fix is its own commit with
  a "Phase 3.5 bug fix" prefix.
- Reports go in `reports/`; data goes in `data/historical/`;
  manifests/audits go in `docs/`. The line is: anything large
  enough to gitignore goes outside `docs/`.

---

## What Phase 3.5 explicitly does NOT do

- Tune profile thresholds against backtest results (that would break
  falsification discipline)
- Run a live or paper trade (out of scope; Phase 5+)
- Build a dashboard (Phase 4)
- Try different strategy profiles after seeing Track B's results
  (also breaks falsification — separate phase if needed)
- Send any alerts or notifications

---

## Open question — when to decide if download is too slow

If the Tier-2 download in Phase 3.5.3 stalls below 1 ticker-month
per hour, that's < 1 GB/hour, and the full download would take 50+
days. Decision rule:

- < 1 ticker-month/hour for 4+ hours → STOP, contact ThetaData
  support to verify Pro plan bandwidth allowance
- ThetaData confirms throttling → Phase 3.5.3 splits into multi-
  month batches with explicit rate adjustment
- ThetaData says throughput is correct → reduce universe to Tier-2
  high-priority subset (top 25 by liquidity), document reduced
  universe in Phase 3.5.3 closeout

The reduced-universe path is acceptable; the project doesn't pause
on download speed. But a 50-day download on a personal machine isn't
realistic.

---

## What you (Berkay) need to do before Phase 3.5 starts

1. Confirm both API keys are working locally (Phase 3.5.1 will
   verify; this is the pre-check).
2. Confirm laptop has 200 GB free for `data/historical/`.
3. Set up Theta Terminal to launch on system startup if Phase 3.5.3
   download will run overnight.
4. Decide: full Tier-2 (51 tickers) or reduced (top 25)? Default is
   full; reduced is fallback if download proves too slow.

---

## Calibration philosophy reminder (from Phase 3.4)

This phase **RESPECTS** the calibration. Profile defaults set in
Phase 3.4 are the test conditions. We do not retune mid-test.

If Phase 3.5.6's verdict is "edge rejected", the right next step is
NOT "let's tune the thresholds and rerun on the same data". That's
curve-fitting and produces fake edge. The right next step is either
(a) accept that this strategy doesn't work as formulated, or (b)
form a NEW hypothesis with NEW thresholds and run a NEW backtest
on a different time period.

Falsification discipline. We pinned the rules in Phase 3.2.4. We
respect them in Phase 3.5.

---

This document is the contract for Phase 3.5. It will not be revised
mid-implementation; if a decision needs revisiting, that discussion
happens between sub-phases, not within them.
