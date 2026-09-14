# Study D — Result (fresh-window run, 2024-05-01 → 2025-04-30)

**Pre-registered:** `docs/preregister_D.md` (`2e3094a`) + addendum
`docs/preregister_D_addendum.md` (`6c189e4`, **causal trailing iv_pct = decision
metric**, inherited look-ahead = reference-only). Single run, no re-tuning, no
post-hoc window/ticker/threshold change. Supersedes the prior BLOCKED status.

## Top-line verdict: **WEAK / BORDERLINE — does NOT by itself justify C**

The conditioning's **look-ahead-free incremental edge over blind vol-selling is
not statistically significant** on the fresh window. It passes the *letter* of the
pre-committed `t_cond`/DSR gate, but the metric that actually tests D's question —
does conditioning **beat** blind vol-selling — is insignificant (Welch t **+1.47**,
< 2) and has **degraded out-of-sample** vs the in-sample causal reference
(+0.51 → +0.22). **Decision returns to Berkay. C is NOT started.**

---

## 0. Data (validated, `scripts/validate_fresh_window.py`)

24/24 tickers present, full 2024-05..2025-04 coverage, 5514 panel-eligible obs
(burned-panel comparator: 5342). Only gap: 6 heavy days dropped on bulk
JSONDecodeError (TSLA 5, CRWD 1) = 0.1% — not a broken universe. Built panel after
the inherited gamma/iv liquidity drops: **5301 valid ticker-days**.

## 1. The single run

```
panel: 5301 valid ticker-days · SELL(inherited)=1078 · SELL(causal)=1566
n_UNCOND (non-overlap) = 278 · n_COND(causal) = 101 · n_COND(inherited) = 61
```

## 2. PRIMARY — causal trailing iv_pct (THE decision metric)

| Arm | Sharpe | t_arm | n |
|---|---|---|---|
| UNCOND (blind vol-sell, all obs) | +1.44 | +6.75 | 278 |
| COND (long-gamma + high-IV, causal) | +1.66 | +4.69 | 101 |

- **SPREAD_causal = +0.22** · IC = +0.094 · **t_cond = +4.69** · **DSR = 1.000**
- **Welch t (COND − UNCOND) = +1.47**  ← the value-add significance; **< 2, NOT significant**

## 3. REFERENCE ONLY — inherited full-window iv_pct (look-ahead, decides nothing)

| Arm | Sharpe | t_arm | n |
|---|---|---|---|
| UNCOND | +1.44 | +6.75 | 278 |
| COND (inherited look-ahead) | +1.86 | +4.09 | 61 |

- SPREAD_inherited = +0.42 · IC = +0.155 · Welch +2.07 · DSR 0.999. The look-ahead
  variant looks "more significant" (Welch 2.07 vs causal 1.47) — confirming the
  look-ahead inflates the apparent edge, exactly why the addendum demoted it.

## 4. Why WEAK, not SURVIVED (honest reading of the frozen gate)

By the **letter** of the pre-committed addendum gate the causal arm passes all
three (`SPREAD>0` ✓, `|t_cond|≥2` ✓ 4.69, `DSR>0.95` ✓ 1.000) → mechanically
"SURVIVED". **But that gate is weak and I will not hide it:**

- `t_cond` only tests that the conditioned cell is profitable — it is, but **the
  *unconditioned* cell is even more so** (Sharpe +1.44, t +6.75). The vol-risk
  premium is strong *everywhere*; selling vol blindly already works.
- D's real question is whether conditioning **adds** information. That is the
  **COND−UNCOND Welch t = +1.47**, which is **not significant**.
- Out-of-sample decay: causal spread **+0.51 (in-sample) → +0.22 (fresh)**; causal
  Welch **+1.88 → +1.47**. The conditioning edge is shrinking, not replicating.

| Causal metric | In-sample (burned) | Fresh 2024 |
|---|---|---|
| COND−UNCOND spread | +0.51 | **+0.22** |
| Welch t (cond−uncond) | +1.88 | **+1.47** |
| t_cond | +4.76 | +4.69 |
| DSR | 1.000 | 1.000 |

**Honest verdict: WEAK / borderline.** Positive-signed, look-ahead-free, but the
incremental edge over blind vol-selling is statistically insignificant and
decaying OOS. Per the pre-reg's "weak/borderline → no automatic green light;
decision to Berkay." **C is not started; B and C remain gated.**

One thing the run *does* establish cleanly: the **unconditioned** vol-risk premium
is large and highly significant on a fresh window (Sharpe +1.44, t +6.75) — the
edge is in selling vol, the gamma/IV *conditioning* adds little look-ahead-free.

## 5. Discipline check

- [x] Single run, single window, single hypothesis (n_trials=1).
- [x] Conditioning inherited verbatim; no threshold re-tuned.
- [x] Causal metric primary; inherited look-ahead reference-only.
- [x] Frozen gate reported by its letter (SURVIVED-by-t_cond) AND the honest
      caveat (Welch insignificant) — verdict NOT shaved either way.
- [x] No window/ticker/threshold change after seeing the number.
- [ ] **Push pending — Berkay sees this report first. C NOT started.**
