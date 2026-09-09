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

*(appended when the sub-phase closes — Phase 3.6.4.)*
