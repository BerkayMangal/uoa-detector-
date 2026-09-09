# Phase 3.6 — Daily Screener Digest (acceptance contract)

Status: **FROZEN** (per D1) once code lands against it.
Owner: Berkay. Author: Claude (Opus 4.8).

---

## 1. Objective

Give Berkay a single, daily, ranked, readable digest of the *actionable*
candidates the full pipeline (Unusual Whales + ThetaData enrichment,
M21–M28) surfaces — a **candidate funnel** he applies his own trading
judgment to, not a raw per-record dump.

The digest answers one operational question each morning: *"of everything
the detector saw, which US names are worth me looking at for a long-option
trade today, and in what order?"*

## 2. Honest framing (non-negotiable)

This is **decision-support**, not proven edge. Phase 3.5 (real backtest)
has not returned a verdict; nothing in this project is yet allowed to
claim profitability (CLAUDE.md: "Make optimistic claims about edge …" is
in *never do*).

Every rendered digest carries this header verbatim:

> Decision-support candidates — ranked by confluence score. Apply your own
> judgment; these are NOT proven-profitable signals.

The ranking is by **confluence score** (the combined score the pipeline
already computed). A higher rank means "more independent sources agreed",
NOT "more likely to be profitable". The screener adds **no** new scoring,
**no** new thresholds, and does not re-rank on anything the pipeline did
not already decide.

## 3. Scope

A new `screener` CLI command plus a digest renderer module. Concretely:

1. **`screener` command in `cli.py`.** Reuses the exact pipeline/source
   wiring the existing `run` command uses (same `--source`, `--profile`,
   `--store`, `--feeds`, and the historical/synthetic flags), but instead
   of streaming raw records it **collects** every `SignalDecisionRecord`
   and renders a digest. `--source` defaults to `live` (this is a morning
   tool). The existing `run` command is **not** modified.

2. **`src/uoa_detector/observability/digest.py`.** Pure rendering:
   - **Filter** to actionable candidates: `size.max_r > 0` **and**
     `decision.label not in` the non-actionable set
     (`IGNORE_NOISE`, `LIKELY_CLOSING_OR_NOISE`, `POST_EVENT_NOISE`,
     `PENALIZED_BELOW_THRESHOLD`, `REJECTED`). The two conditions are
     belt-and-suspenders: `max_r > 0` already excludes DISCARD-bucket
     labels, and the label set excludes any noise label that a future
     profile might give a non-zero R.
   - **Rank** by `event.combined_score_post_penalty` descending. This is
     the pipeline's final post-penalty combined score — the same number
     the labeler and sizer saw.
   - **Render** a fixed-width table to stdout and a markdown twin to
     `--report-path` (default `reports/screener_<YYYY-MM-DD>.md`).
   - **Columns:** rank | ticker | side | label | score | contract |
     max_r | top reasons. Contract is `C 250 @ 2026-07-18 (7DTE)`.
   - **Empty result:** print "No candidates cleared thresholds today."
     under the header — never crash.

3. **Determinism.** Rendering is a pure function of its input records.
   Ties in score are broken deterministically by
   `(ticker, option_type, strike, expiry, event_id)`.

## 4. Design decisions (the two that affect what a trader acts on)

### 4.1 "LONG vs short" — the SIDE column

The detector only surfaces **option-buying** unusual activity (long
premium / long convexity); it has no notion of a short-option signal.
Therefore the SIDE column is always `LONG`, meaning *long the option
contract*. The **directional view (bullish vs bearish) is carried by the
call/put in the CONTRACT column** — `C` = long call (bullish), `P` = long
put (bearish). Each digest prints a one-line legend making this explicit:

> LONG = long the option contract (long premium / long convexity).
> Bullish vs bearish view = call (C) / put (P) in CONTRACT.

Rationale: there is no genuine long/short fork to resolve — the system
never emits a short-option signal — so the only real directional
information (C vs P) is rendered prominently and cannot be misread by a
trader. No numeric inference is performed.

### 4.2 Contract detail

Derived directly from the `OptionsPrint`: `option_type` → `C`/`P`,
`strike` (integral strikes render without a decimal tail), `expiry` (ISO
date), `dte` → `{dte}DTE`. No derivation, no lookups.

### 4.3 Top reasons

The two largest positive contributors from the record's
`score_breakdown` (penalty entries excluded), formatted `name +0.42`,
ordered by contribution descending with a name tiebreak. The markdown
twin additionally carries the labeler's own `LabelDecision.reason`
string for context.

## 5. Done-when

- `screener` command exists; `run` behaviour is byte-for-byte unchanged.
- Synthetic path (`screener --source synthetic`) renders a digest with no
  credentials and no ThetaData Terminal.
- Digest header is verbatim (§2); profile_id, run timestamp, and count
  are present.
- Empty input renders the empty-state line, exit code 0.
- New unit tests cover: ranking order, filtering (rejected + zero-size
  dropped), empty case, table columns present, markdown file written,
  determinism (stable tiebreak).
- `uv run pytest -q && uv run mypy --strict src/ && uv run ruff check .`
  all green on every commit (D2).

## 6. Out of scope

- Any new scoring, weighting, or threshold (D8). The screener reads the
  pipeline's existing combined score only.
- Any profile edits (`profiles/*.yaml` untouched).
- Live-mode execution in tests (needs credentials / market hours).
- PnL, fills, alerts, auto-execution — Phase 4+ territory.
- Historical/backtest metrics — that is Phase 3.5's job, not the digest's.

## 7. Closeout (paket-mode)

**Status: KAPALI.** `screener` command shipped; `run` untouched. Synthetic
path renders a digest with no credentials.

Commits (branch `phase-3.6-screener`):

- `Phase 3.6.1` — frozen acceptance contract (this doc, §1–§6).
- `Phase 3.6.2` — `observability/digest.py` renderer + `CollectingWriter` +
  observability exports + 9 unit tests.
- `Phase 3.6.3` — `screener` CLI command + 7 integration tests.
- `Phase 3.6.4` — this closeout.

Green state (whole tree, HEAD): `uv run pytest -q` → 1477 passed, 20 skipped
(credential-gated smokes); `uv run mypy --strict src/` → clean, 113 files;
`uv run ruff check .` → clean.

New tests: 16 total.
- Unit (`tests/unit/test_digest.py`, 9): ranking order; zero-size + noise
  label filtering; empty result (stdout + markdown); all-filtered → empty;
  columns/header/legend present; markdown shape; contract + LONG-side
  derivation (incl. a put → `P`); deterministic tiebreak on equal scores;
  top-reasons excludes penalties and caps at 2.
- Integration (`tests/integration/test_cli_screener.py`, 7): synthetic
  digest to stdout; markdown report written (nested dir created); profile_id
  in header; `--data-dir` / `--live-tickers` / bad-source validation;
  regression guard that `run --output json` still streams 8 raw records.

Judgment calls:

1. **SIDE column is always `LONG`.** The detector only surfaces
   option-buying flow; there is no short-option signal to disambiguate.
   `LONG` = long the contract; bullish/bearish is the C/P in CONTRACT, made
   explicit by the printed legend. (Acceptance §4.1.) Not a blocker → not
   escalated.
2. **Rank on `combined_score_post_penalty`.** The pipeline's final,
   post-penalty score — the number the labeler and sizer already used. No
   new scoring, no new thresholds (D8).
3. **Dual filter** (`max_r > 0` AND label ∉ noise set) is intentional
   belt-and-suspenders: `max_r > 0` handles today's DISCARD-bucket zeros,
   the label set guards against a future profile giving a noise label a
   non-zero R.
4. **`CollectingWriter`** added to `output.py` implementing the existing
   `DecisionRecordWriter` Protocol, so the screener reuses the pipeline's
   writer rail unchanged. Its `close()` retains the buffer (the pipeline
   owns the writer's lifecycle and closes it before the CLI reads records).
5. **structlog → stderr** in `screener` so stdout carries only the digest
   (matches `run`'s stdout-hygiene convention).
6. **Top reasons = top-2 positive `score_breakdown` contributors**
   (penalties excluded); the markdown twin additionally carries the
   labeler's `LabelDecision.reason` string.

Note on the task brief vs. code: the brief referenced
`LabelDecision.reasoning`; the real field is `LabelDecision.reason` (single
string) — used as the "Labeler note" column in markdown.

Sıradaki: Phase 3.5 remains the gating question (does the strategy have an
edge). The screener is decision-support tooling and makes no edge claim;
its ranking is confluence, not expected PnL.
