# Repo audit — uoa-detector (2026-06-24)

Comprehensive, evidence-based audit (logic / data-and-inference / bug / dead-code
/ efficiency). Every claim below was produced by **running** code, not reading it.
Findings are labelled **CONFIRMED** (verified by execution/hand-calc) or
**SUSPECTED** (logical, not fully provable in-repo), and **FIXED** or **FLAGGED**
(behaviour/inference-changing → left for a deliberate decision, not silently
changed).

## Verdict

**No CRITICAL defect.** No look-ahead bias, no data-corruption, no
signal-inverting bug. The one SUSPECTED critical (live gamma sign could be
inverted) was investigated and **resolved as correct**. The detector core
(125 files, 24.5k LoC, 1611 tests, mypy --strict clean) is sound. The real
findings are: one **quant over-claim** (now corrected), two **measured
efficiency wins** (fixed), three small **dead-code** removals (fixed), and a
handful of **live-path behaviour gaps** that are flagged for your call.

### Final gate (evidence)
```
ruff check .            → All checks passed!
mypy --strict src/      → Success: no issues found in 125 source files
pytest -q               → 1611 passed, 20 skipped
vulture (90% conf)      → only ws_path (intentional NotImplementedError stub)
coverage (overall)      → 92% (webapp live/background code lower — see M-3)
```

---

## FAZ 0 — Map + tooling

- `src/uoa_detector` 125 files / 24,519 LoC · `webapp` 9 / 1,445 · `scripts` 14 / 2,603 · `tests` 123 files.
- Entry points: `webapp.main:app` (FastAPI — screener / gamma board / journal + lifespan worker), `uoa_detector.cli` (detector CLI), `scripts/*` (research + data jobs).
- Tooling present: ruff 0.15.12, mypy 1.20.2, pytest 9.0.3.
- **Missing tools (installed for this audit):** `pytest-cov`, `vulture` — neither was in the venv; coverage + dead-code analysis were not part of the standing toolchain. (Recommendation L-2.)

---

## FINDINGS

### HIGH

**H-1 — Quant over-claim: the gamma "vol effect" does NOT survive its own robustness test. [CONFIRMED · FIXED]**
The project treated H1 ("extreme-gamma names move more") as the surviving gamma
result and built the gamma map's rationale on it. The study only reported the
**full-sample t=2.60**; applying the SAME non-overlap gauntlet that killed the
directional + pinning claims:
```
H1 vol FULL:        diff=+0.0239  t=2.60   => CONFIRMS
H1 vol NON-OVERLAP: diff=+0.0232  t=1.08   => does NOT survive — not robust
```
Inconsistent rigor (non-overlap applied to the directional claim but not to H1).
**Fix:** `scripts/study_gamma_regime.py` now computes + prints the non-overlap H1
(exposes t=1.08); `webapp/gamma.py` module docstring corrected — it no longer
claims the vol effect as established and states plainly that the map is *pure
structural context, never a signal*.

**H-2 — Vol-premium edge: the headline t=10.16 is overlap-inflated. [CONFIRMED · already honest in code, documented]**
The one surviving edge (sell vol in long-gamma + high-IV, `study_vol_premium.py`)
is real but its full-sample **t=10.16 is fiction** — n=1082 overlapping 20-day
windows, massively autocorrelated. The honest figure is the **non-overlap
t=2.64 (n=63)**, which the study already prints. It also **sells tail risk**
(skew −14.17; worst obs LCID 2025-08-05 implied 98% vs realised 778%), and the
11-month sample contained **no market-wide vol crash**, so the in-sample mean
(8.5 vol pts) overstates the post-crash Sharpe. **Multiple testing:** ~30+
headline statistics across the 3 studies; the SELL cell's raw non-overlap
p≈0.008 × ~30 Bonferroni ≈ 0.25 — it would NOT clear FDR as an isolated test. It
survives only because it is *corroborated* (positive in both halves, monotone in
IV percentile, 70.6% sign test, not tail-driven: winsorising raises t). The code
(`gamma.py` `vol_signal`/`vol_label`, the `/gamma` board header) **already**
caveats this correctly (SELL = only survivor, defined-risk, tail risk, no crash).
**No code change needed; size off t=2.64, never quote t=10.**

**H-3 — `STANDARD_UOA` is unreachable in the LIVE path. [CONFIRMED · FLAGGED]**
Chain: live `_run_live` (cli.py) builds the pipeline with the default
`PipelineContext` → `NoOpMedianTradeSizeProvider` → `relative_premium_score`
stays `None` → labeler gate `(None or 0.0) >= 0.5` is always False. The
"large-UOA" arm of `HIGH_CONVICTION_SEQUENCE` is likewise dead live (only the
sweep/iso arm can fire). **Does NOT affect `compute_combined_score`** (relative
premium is not a combined-score axis) — only *labelling*. Already surfaced in the
UI (relative-premium is shown as `n/a · live tier`). **Flagged** (behaviour
decision): wire a live median provider, or accept STANDARD_UOA as backtest-only.

**H-4 — Dashboard issued 6 serial DB round-trips per request (~3.1s). [CONFIRMED · FIXED]**
Remote Railway Postgres ≈178ms RTT; server-side query ≈1.3ms — wall time is
round-trip *count*, not SQL. `count()` re-queried a number `runs()` already
returns; `tickers`+`labels` were two separate DISTINCT queries.
**Fix:** drop `count()` (use `RunInfo.count`); merge tickers+labels into one
cross-DB `SELECT DISTINCT ticker,label` (`repo.ticker_label_options`). Removed
the now-dead `repo.tickers/labels/count`. **Measured before/after (live
Postgres, run live-2026-06-22, 1319 sig):**
```
BEFORE (6 queries): median 3062ms
AFTER  (4 queries): median 2142ms   → 921ms / ~30% faster
```

### MEDIUM

**M-1 — `_is_opening_pending` ignores M27's computed `opening_closing_score`. [CONFIRMED · FLAGGED]**
`labeler.py` decides opening-vs-closing from `fill_side` alone (docstring: "until
Module 27 lands in Phase 3"), while M27 *has* landed and writes
`opening_closing_score` — which the labeler never reads (only the M28 validator
does). Stale heuristic; the label can contradict the OI-delta data. Low blast
radius (gate 15, near the bottom of precedence). **Flagged** — possibly
intentional (M27 → M28 by design); confirm against the M27 acceptance doc.

**M-2 — Two flip implementations compute different quantities. [CONFIRMED · documented]**
`gamma.py` flip = hypothetical-spot scan (the SpotGamma "gamma flip"); 
`gamma_live.py` flip = the *strike* where cumulative-across-strike GEX crosses
zero. Both interpolators are arithmetically correct but answer different
questions, so the displayed live flip ≠ the backtested flip. They cannot be
unified — live UW gives pre-computed per-strike GEX (no raw gamma/OI to
spot-scan). Cosmetic context field, not an edge driver. **Documented**, not
changed (changing the live formula would alter a displayed inference).

**M-3 — `compute_gamma` 161-pt spot-scan was 61% of its cost. [CONFIRMED · FIXED]**
Offline only (studies call it ~6000×; daily snapshot). **Fix:** vectorised the
scan into one `(161 × n_contracts)` broadcast. **Verified BIT-IDENTICAL output**
(`max|new−old vals| = 0.00e+00`; flip=370.89, walls=380.0 match the Phase-4.10
reference exactly; 12 gamma tests pass). Timing: scan ~10ms→~3ms, compute_gamma
~17.5ms→13.2ms (~25%); the studies' ~105s batch → ~80s. Zero behaviour change.

**M-4 — `webapp/` is NOT in the mypy gate (26 type errors). [CONFIRMED · FLAGGED]**
`mypy --strict` covers only `src/`. `mypy webapp/` reports 26 errors:
~16 are import-path false-positives (mypy run without `mypy_path=src`); the rest
are the `_safe()`-returns-`object` pattern (callers `.values()`/`.run_id` under
`# type: ignore`) and `directional_excess`'s tuple-narrowing (runtime-safe — a
`if any(p is None or p==0…): return None` guard precedes the division;
**CONFIRMED safe**). No runtime bug, but the live-serving code has no type gate.
**Flagged** (quality): add `webapp` to mypy with `mypy_path = src` and make
`_safe` generic to delete the ignores.

**M-5 — webapp coverage gap. [CONFIRMED · noted]**
Overall coverage 92%, but `webapp/worker.py` 29%, `pricing.py` 59%, `main.py`
70% — the live/background paths are hard to unit-test without live feeds. The
*pure logic* (journal metrics, gamma math, flow mapping, explanations) is
unit-tested and the routes have smoke tests. Acceptable; flagged for awareness.

### LOW

**L-1 — Dead code removed. [CONFIRMED · FIXED]** (zero references verified before removal)
- `webapp/journal.py` `_verdict(n_closed=…)` — param only in the signature, unused in the body. Removed (+ updated 4 test calls).
- `src/uoa_detector/backtest/cell_runner.py` `noop_pnl_signal_to_trade` — public fn, zero refs repo-wide. Removed.
- `src/uoa_detector/sources/thetadata/client.py` `ThetaDataRateLimitError` — exception never raised/caught. Removed.
- (Not removed — intentional: `ThetaDataClient.stream_ws` `ws_path` is an unused param of a documented `NotImplementedError` reserved stub.)

**L-2 — Live flow source can never emit `above_ask`. [CONFIRMED · FLAGGED]**
`flow_poll._fill_side_from_prems` returns only `at_ask`/`at_bid`/`unknown`; UW
flow-alerts collapse all ask-dominant fills to `at_ask`. No gate breaks
(`at_ask` is in every relevant set), but the strongest-aggressor distinction is
lost live. Minor fidelity. **Flagged** — could derive `above_ask` from a strong
ask/bid side-premium ratio.

**L-3 — Toolchain: add `pytest-cov` + `vulture` to dev deps.** Neither was installed; coverage/dead-code were not standing checks.

**L-4 — `ruff` RUF002/003 (ambiguous unicode ×/− in docstrings/comments).** Intentional typography; already ignored in `pyproject.toml`. No action.

---

## QUANT correctness (mandatory: look-ahead / overfit / walk-forward)

- **Look-ahead: CONFIRMED CLEAN (all 3 studies).** Chain `spot` == spot-series EOD close for day d (verified to 1e-4 across 5 dates). Forward returns are strictly future: `close[d+k]` / `r=close[close.index>d]`. Inner joins on normalised dates — no misalignment. The EOD-d-OI → predict-d+1 design is correct; no leak anywhere.
- **Math: CONFIRMED correct.** BS gamma (r=0, `φ(d1)/(S·σ·√t)`) matches reference to 1e-12; GEX convention `call_gex − put_gex` matches the tested M21 provider and SqueezeMetrics; VRP = implied − realised, both √252-annualised; market-neutral cross-sectional demean genuinely forces each date's cross-section to 0; non-overlap sampling (`iloc[::5]`/`::20`) is genuinely non-overlapping for the 5d/20d horizons (panel rows are consecutive trading days).
- **Overfit / FDR: the surviving vol-premium is real but small and fragile** (see H-2). Direction + pinning rejections are SOUND (LCID-artifact + sign-flipping nulls correctly exposed by the studies' own panels). The gamma "vol effect" is NOT robustly established (H-1).
- **Live gamma sign (was SUSPECTED critical): RESOLVED CORRECT.** UW delivers `put_gex` pre-signed negative (422/422 negative on SPY); `gamma_live` uses `call_gex+put_gex`, byte-identical to the Phase-3-tested M21 provider convention. SPY reading net-short is a real market state, not a sign flip.

---

## Summary — fixed vs flagged

**FIXED (bug / dead-code / efficiency, all with evidence + green tests):**
H-1 (quant over-claim now exposed + docstring corrected) · H-4 (dashboard −30%,
measured) · M-3 (gamma vectorised, bit-identical, −25% offline) · L-1 (3 dead
items removed).

**FLAGGED (behaviour/inference-changing — your call, not silently changed):**
H-3 (STANDARD_UOA unreachable live) · M-1 (`_is_opening_pending` ignores M27) ·
M-2 (live vs backtest flip differ) · M-4 (webapp not in mypy gate) · L-2
(no `above_ask` live).

**Tests:** 1611 passed, 20 skipped — no regression. **Lint/type:** clean.
**Efficiency:** H-4 −921ms (−30%) and M-3 bit-identical −25%, both measured.
