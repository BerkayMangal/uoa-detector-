# Study D — Fresh-Window Result (TEMPLATE — format frozen before the run)

**Purpose:** lock the reporting format BEFORE any fresh-window number exists, so
filling it in is mechanical and no metric is chosen/emphasised post-hoc. When the
single run completes this template is filled verbatim and replaces the BLOCKED
body of `docs/study_D_result.md`. Pre-registration: `docs/preregister_D.md`
(`2e3094a`) + addendum `docs/preregister_D_addendum.md` (`6c189e4`).

> **Decision metric = the CAUSAL / trailing-iv_pct variant (addendum).** The
> inherited full-window-rank variant is REFERENCE-ONLY (it carries in-window
> look-ahead) and never decides the verdict.

---

## 0. Data validation (gate — from `scripts/validate_fresh_window.py`)

- All 24 universe tickers present + non-empty in chain_snapshots_2024 + spot_series_2024? **____**
- Window coverage 2024-05-01..2025-04-30, missing-weekday outliers (heavy days
  dropped on JSONDecodeError): **____**
- Total panel-eligible obs (pre-conditioning upper bound): **____**
- Verdict on data: **CLEAN / BROKEN** — if BROKEN, D is **not run** (pre-reg).

## 1. Panel built

- `panel: ____ valid ticker-days · SELL(primary/inherited)=____ · SELL(causal)=____`
- n_COND (causal, non-overlap) = **____** · n_UNCOND (non-overlap) = **____**

## 2. PRIMARY — causal trailing iv_pct (the verdict metric)

| Arm | Sharpe | t | n |
|---|---|---|---|
| UNCOND (blind, all obs) | **____** | **____** | **____** |
| COND (long-gamma + high-IV, causal) | **____** | **____** | **____** |

- **SPREAD_causal = Sharpe(COND) − Sharpe(UNCOND) = ____**
- IC (point-biserial) = **____** · Welch-t(cond−uncond) = **____** · DSR(n_trials=1) = **____**

## 3. REFERENCE ONLY — inherited full-window iv_pct (NOT the verdict)

| Arm | Sharpe | t | n |
|---|---|---|---|
| UNCOND | **____** | **____** | **____** |
| COND (inherited look-ahead) | **____** | **____** | **____** |

- SPREAD_inherited = **____** (shown only to compare with the documented
  in-sample +0.80 / causal +0.51; carries look-ahead, decides nothing).

## 4. Verdict (addendum §2 thresholds, on the CAUSAL arm)

| Outcome | Condition | → |
|---|---|---|
| **DEAD** | `SPREAD_causal ≤ 0` | conditioning was a burned-panel artifact; **B & C dead-on-arrival**, not started. Done. |
| **WEAK** | `SPREAD_causal > 0` but (`\|t_cond\| < 2` or `DSR ≤ 0.95`) | not robustly replicated; **no green light**; decision returns to Berkay. |
| **SURVIVED** | `SPREAD_causal > 0` and `\|t_cond\| ≥ 2` and `DSR > 0.95` | look-ahead-free info replicated; **STOP — design C together** (do not start C). |

**RESULT: ______**

Old causal in-sample (burned panel) was +0.51 (Welch-t 1.88). Fresh 2024 causal
spread = **____** → **<one honest sentence: replicated / artifact / borderline>**.

## 5. Discipline check (must all be true)

- [ ] Single run, single window (2024-05-01..2025-04-30), single hypothesis (n_trials=1).
- [ ] Conditioning inherited verbatim; no threshold re-tuned on fresh data.
- [ ] Causal metric was primary; inherited look-ahead reference-only.
- [ ] No window/ticker/threshold change after seeing the number.
- [ ] pytest green; result committed; **push only after Berkay sees this report.**
