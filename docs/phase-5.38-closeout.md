# Phase 5.38 closeout — fixed-candidate stress audit

KAPALI — 504 documented stress simulations shipped to the private strategy desk.
Owner authorizes continued research and deployment in chat, 2026-10-28 Istanbul.
No new winner, threshold change, API call, broker order or PAPER promotion.

## Result and decision

The existing recovery hypothesis is FRAGILE, not validated alpha. Baseline 37
closes net +$1,002.40; fixed-ledger removal of its best single win leaves +$483.37,
removal of its best three wins leaves **-$239.04**. Largest win is 12.13% of gross
positive P&L. July signals contributed +$1,019.82, August -$621.30, September
+$603.88. A rerun omitting July SIGNALS, reallocating slots/cash, yields **-$103.21**;
it does not remove all trades closing in July.

At baseline timing, 50bp PER SIDE leaves +$81.32. Two additional entry sessions
and 50bp/side yield -$441.93, 36 trades. One additional session at baseline costs
yields +$1,825.83, 36 trades: this observed improvement does NOT change the frozen
entry rule. Omitting each of 69 non-SPY names gives net range +$350.10 (CLSK
omitted) to +$3,049.22 (APP omitted), with cash/slots fully rerun. These are
diagnostics, not proposals to optimize exclusions on the already-seen test.

All six family results remain visible. An originally rejected strategy can turn
positive under a diagnostic delay; that does not resurrect it or create a new
PAPER arm. The old joint search audit and its failure to establish significance
remain unchanged. Prospective independent-unit measurement is still required.

## Artifact and implementation

`profiles/stress_v1.yaml`: delays 0/1/2 × costs 5/10/20/50bp; ledger removal 1/3.
Six winners × (12 delay/cost + 69 name + 3 signal-month exclusions) = 504. All six
baseline metrics, ledgers, equity and daily-excess paths exactly match the old
artifact before reporting results. Delayed original eligibility, ranks and ATR
stay fixed, prices do not move, pre-split signals cannot be imported, exits stay
inside the old split, missing prices are not substituted. Horizon is measured
from delayed entry; different trade counts are explicit.

Source SHA matches the ORIGINAL frozen Study F source. Reserved Study G and
synthetic data were not read for research. No additional history was accessible.
Summary hash:
`d752b4d60747c6a7600e75fc03f889a63620fb555f96f832e238c5b2e8ce5a7f`.
All 504 full ledgers/equity paths stored in deterministic `replays.jsonl.gz`.
Load-time dependency/profile/base-report checks preserve artifact integrity;
the web path reads the compact summary and implementation checksums, never
historical price data or the optimizer. Supplemental failure preserves the
existing desk. Full replay integrity is independently checked by the test gate.

Archived tournament implementation, profiles and all original artifacts remain
byte-for-byte unchanged. The forward v2 runtime is unchanged. Existing private
auth/no-store/noindex and public SEO are retained; new section reuses existing
styles without a new asset/cache version.

## Verification and delivery

- **16 new behavioural cases**: frozen delayed rank/ATR, independent cash/P&L
  arithmetic, split/exit boundary, missing open, cash reallocation, signal-month
  semantics, concentration and unknown outcomes, source rejection, exact six
  legacy replays, offline overwrite guards, all 504 ledger/equity reconciliations,
  four artifact dependency-tamper cases, actual private page and failure isolation.
- Full hermetic gate: **4974 passed, 52 key-gated skips**; strict mypy **260 source
  files**, ruff, lock and diff clean.
- Actual frozen reports rendered in local Chromium at 1440/390px, all folds
  collapsed/expanded; no overflow or JS errors, zero axe WCAG A/AA violations.
  Local forward DB/provider content is a labelled fixture, not production data.
- Commit: Phase 5.38.1; green PR/main CI and matching Railway SHA required before
  reporting deployment. No authenticated production visual inspection claimed.

No scoring module changed; score truth tables do not apply. Next: expose forward
data readiness, so missing coverage is distinguishable from no qualifying signal.
