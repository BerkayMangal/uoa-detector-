# Phase 3.6 — Self-derived confluence (acceptance)

**Status: frozen on commit (D1). Implement against this; do not edit to
match what you ended up doing.**

## Why this phase exists

Phase 3.5 proved the pipeline runs end-to-end at scale and that the
falsification machinery is correct, but the Track B verdict is gated on
Unusual Whales (UW) historical access (B0). UW has been unresponsive for
~3 weeks. Phase 3.6 removes that gate **without abandoning the confluence
thesis**: rebuild the confluence enrichment axes from the ThetaData option
chain we already own (290.9M prints, OI, IV, strike, expiry, parity spot)
instead of from UW.

The thesis is unchanged — *edge requires multi-source confluence*. Only
the **data source** behind the enrichment changes. The fusion stages
(M21–M28), the 4-cell runner, the falsification engine (3.5.6), the
sanity-audit tooling (3.5.7), the vectorised replay (3.5.5.14) and the
logging fix all stay as-is.

This is a **new hypothesis track**, distinct from Track B (UW-fed). It is
NOT a retune of v5_gamma_squeeze — see the threshold discipline below.

## Axis map — UW → ThetaData-derived

| Confluence axis | Module | Plan | Phase |
|---|---|---|---|
| Unusual flow | (core) | already ThetaData | — |
| Spot action | (core) | already ThetaData (parity) | — |
| **Dealer gamma (GEX)** | M21 | **self-derive from chain (this phase)** | 3.6.1–3.6.3 |
| IV regime | M-iv | self-derive from our per-contract IV | 3.6.4 |
| OI delta | M-oi | self-derive from our OI | 3.6.4 |
| Event calendar | M28 | free earnings CSV (committed, historical fact) | 3.6.5 |
| Dark pool | M-dp | **DEFERRED — genuinely UW-only (FINRA ATS)**; runs NoOp | — |
| Sector peer flow | M25 | DEFERRED — derivable later from our own multi-ticker flow; runs NoOp | — |

6 of 8 axes are reachable without UW. The 2 deferred axes run on their
NoOp providers. Per the existing falsification discipline a NoOp/illiquid
axis yields a `None` sub-score handled by `sub_score_missing_behavior`; it
must NOT be read as a REJECTED verdict.

## MVP = GEX first (3.6.1–3.6.3)

The strategy profile is literally `v5_gamma_squeeze`; gamma-flip proximity
is its central feature. So the MVP is the **dealer-gamma (M21) axis**:
self-derive net gamma + flip strike, wire it into the fusion, and run the
4-cell. Combined with the core ThetaData axes (flow, spot, sweep,
convexity) this is already a genuine multi-source confluence cell with no
UW. IV/OI/catalyst expand the confluence in 3.6.4–3.6.5.

## GEX methodology (frozen)

The M21 stage already consumes a `DealerPositioningProvider`
(`aggregate_for_ticker(ticker, at) -> DealerExposureAggregate(
net_gamma_dollars, flip_strike)`); its scoring (net-short AND spot-near-
flip → 1.0, etc.) stays in the stage. We only supply a new provider that
computes the aggregate from the chain. The provider Protocol docstring
already anticipates this ("User's own derivation from OI + classification
... snapshot data").

**Per-contract gamma** (Black–Scholes, calls and puts identical):

```
d1    = [ln(S/K) + (r + σ²/2)·T] / (σ·√T)
gamma = N'(d1) / (S·σ·√T)              N'(x) = e^(−x²/2)/√(2π)
```

- `S` = parity spot as-of, `K` = strike, `σ` = contract IV as-of,
  `T` = (expiry − as_of_date)/365 in years.
- `r = 0.0`. Gamma's sensitivity to the risk-free rate is third-order for
  short DTE (the download is DTE ≤ 60); a deliberate, documented
  simplification, not a tunable.
- Degenerate inputs (`T ≤ 0`, `σ ≤ 0`, `S ≤ 0`) → contract contributes 0.

**Dealer sign convention (SqueezeMetrics/SpotGamma "naive"):** dealers are
long calls and short puts relative to customer demand, so

```
contract_gex = sign · gamma · OI · 100 · S² · 0.01      sign = +1 call, −1 put
net_gamma_dollars = Σ contract_gex over the ticker's chain as-of `at`
```

`net_gamma_dollars < 0` ⇒ dealers net short (the squeeze-prone state M21
scores). This convention is a theory choice fixed a priori; it is not
fitted.

**Flip strike** = the hypothetical spot level `S*` at which total dealer
GEX crosses zero. Evaluate `GEX(S)` (re-pricing every contract's gamma at
S) over a grid of candidate spots spanning the chain's strike range; the
flip is the lowest sign-change crossing, linearly interpolated. No
crossing (monotone curve) → `flip_strike = None` (M21 treats None as no
proximity bonus).

## Point-in-time correctness (frozen — this is the leakage guard)

- A daily **chain snapshot** is precomputed (3.6.2): for each
  (ticker, trading-day, contract) the **last** OI and IV seen that day,
  plus strike/expiry/type/parity-spot. One compact parquet per ticker.
- `aggregate_for_ticker(ticker, at)` uses the snapshot row for each
  contract whose snapshot date is the **most recent ≤ `at`.date()** — never
  a future day. Same as-of discipline as the UW B2 conversion. No row with
  date > event date may enter the sum.
- This is checked by `sanity_audit.check_lookahead` on the verdict run and
  by provider unit tests (a contract whose only snapshot is after `at`
  must be excluded).

## OI coverage approximation (documented limitation)

Full-chain OI for strikes that did not trade in the window is absent (we
only persist OI on prints). GEX is therefore computed over the **traded
contracts'** OI. Near-the-money strikes — which dominate gamma — trade
actively, so the approximation is acceptable for the proximity signal. If
the MVP verdict is promising, the precision upgrade is a one-time ThetaData
full-chain daily-OI download (we have access); that is a 3.6.x follow-up,
not part of the MVP.

## Threshold discipline (D4 — falsification-critical)

M21 uses three `M21Settings` knobs, and they split on scale-dependence:

- `flip_proximity_pct`, `extreme_distance_pct` — both operate on the
  flip-proximity *distance* `|spot − flip_strike| / spot`, which is
  **scale-free**. These transfer from v5_gamma_squeeze **unchanged**.
- `short_gamma_threshold` — `is_short_gamma = net_gamma_dollars <
  short_gamma_threshold`. This **is** scale-dependent. v5's value
  (−$50M, calibrated for UW's GEX units) does NOT transfer to our
  self-derived GEX, whose units differ and whose magnitude is biased low
  by the OI-coverage approximation. Reusing −$50M would be wrong.

**Resolution (a priori, no curve-fit):** the MVP sets
`short_gamma_threshold = 0` in v6 — pure **sign** ("dealers net short at
all"), which is scale-free and parameter-free. The "materially short"
nuance is then carried by the flip-proximity requirement: M21 awards 1.0
only when dealers are short **and** spot is near the flip, so a
marginal-but-near setup and a deep-but-far setup both score 0.5, not 1.0.
A distributional threshold — a fixed a-priori percentile of our own
`net_gamma_dollars` distribution across the chain snapshots, computed
**before** the run and frozen (the `compute_medians.py` pattern) — is an
optional 3.6.x refinement if the verdict warrants; it would still be set
from the feature distribution, never from trade outcomes.

For the axes added later (IV regime, OI delta) the same rule binds: every
threshold must be a **scale-free feature cutoff** (sign / percentile /
z-score / price-distance) set a priori from the feature definition, frozen
in the profile **before** the run. No threshold is ever moved after seeing
a backtest result. If results are weak the verdict is "edge rejected /
hypothesis revision", never "retune".

(`net_gamma_dollars` is in USD per 1% spot move — the `·S²·0.01` factor in
the GEX formula — matching the UW convention, so the sign and any future
distributional cut are computed on a well-defined quantity.)

## Profile

New `profiles/v6_thetadata_confluence.yaml` (do NOT mutate the frozen
`v5_gamma_squeeze.yaml`). It is v5_gamma_squeeze's axis config with the
dealer-gamma axis fed by the self-derived provider and the dark-pool /
peer-flow axes marked deferred. Thresholds are copied as-is per the
discipline above.

## Sub-phases

- **3.6.1** — `ThetaDataDealerPositioningProvider` (provider + BS gamma +
  flip + as-of) and unit tests. No wiring yet.
- **3.6.2** — `scripts/compute_chain_snapshots.py` → per-ticker daily
  chain snapshots; provider loads them.
- **3.6.3** — wire the provider into the replay fusion path, add
  `v6_thetadata_confluence.yaml`, run the 4-cell (no `--uw-enrichment`) →
  falsification verdict to `docs/phase-3.6-results.md`, run the 3.5.7
  sanity audit on it.
- **3.6.4** — add IV-regime + OI-delta self-derived axes (expand
  confluence), re-run.
- **3.6.5** — committed earnings-calendar CSV → M28 catalyst axis, re-run.

## Falsification (unchanged machinery)

Same 4-cell + the 3.5.6 falsification engine. Hypothesis: the
ThetaData-derived **fusion** cells materially beat the single-source
cells. The four pinned reject scenarios and the 30-trade sample gate
apply. Deferred (NoOp) axes do not by themselves trigger REJECTED. The
verdict is mechanical and written to `docs/phase-3.6-results.md`.

## Done when

- 3.6.1–3.6.3 shipped; `docs/phase-3.6-results.md` committed with a real
  (non-INSUFFICIENT-by-construction) verdict produced without UW.
- Every commit green: `pytest -q`, `mypy --strict`, `ruff check .`.
- Point-in-time correctness verified by provider tests + the sanity audit.

## What Phase 3.6 does NOT do

- Touch the frozen `v5_gamma_squeeze.yaml` or any Phase 3.5 acceptance doc.
- Tune any threshold against backtest results.
- Build the dark-pool or peer-flow axes (deferred; NoOp).
- Claim the UW-fed Track B verdict — that remains open pending B0. Phase
  3.6 is a parallel, UW-independent track that can stand on its own.
