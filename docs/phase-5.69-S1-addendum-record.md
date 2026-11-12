# Phase 5.69 — S1 exploratory supplement: archival addendum

This separately records the next five US OHLCV adaptations under the owner's
existing authorization for autonomous research. It does not amend the frozen
[first-pilot contract](phase-5.69-us-intraday-research-acceptance.md), v2 protocol,
registry, engine or results. S1 specification files, final reviewed code and the
pre-run seal and complete results are archived. All five families were measured
under the frozen specification; none establishes validated edge.

## 1. Outcome exposure and chronology

The S1 freeze is dated `2026-11-12T16:51:26.074469+00:00`. It follows the start of
v2's first real-label execution at approximately 16:47 UTC. The specification
author states that they had not inspected v2 outcomes when completing S1, and no
S1 outcomes had been inspected. **The S1 runner author had already observed v2
results.** These are distinct roles and exposure statements; one author's lack
of exposure does not make the whole research team outcome-blind.

The root had requested assessment of the ten remaining OHLCV-feasible ideas
before v2's first-label execution, but the final S1 definitions were sealed
later. This is exploratory prespecification before **S1's own run**, not a
global pre-outcome registration, fresh holdout, or confirmatory experiment.
The parent record and prior 5,184 known exploratory configurations remain part
of the search history. The complete historical trial count remains unknown.

The independent nine-symbol intraday dataset overlaps Study G's post-2026-09-19
calendar window. An earlier [archive search](../artifacts/research150-us/evidence/additional-archive-search.json)
fully parsed and hashed Study G's `bars.csv`, including its duplicate checkout:
all columns, including OHLCV strings, were loaded into memory, while only
headers, ticker/date/row counts and digests were analyzed/output. No return
labels, features, strategy outcomes or prescribed 37-feature/five-session
residual-label experiment were computed from those files. V2/S1 input manifests
contain only the independent intraday dataset; Study G's specification and
stopping rule remain unchanged. Metadata inspection
and overlapping price dates preclude a blanket untouched-data claim; no
research-wide price blindness or wholly unopened Study G file is claimed.

## 2. Original source identity

The four originals are preserved byte for byte after any documented lossless
decompression under
[`artifacts/research150-us/supplement-s1/`](../artifacts/research150-us/supplement-s1/archive-manifest.json).
The source-idea digest remains
`5948db58084f89c465a760dd6fbad884d304dde5d2dfac7366b0aff94ebbd7a4`;
the v2 protocol digest remains
`456dfa9b66a3de453c1d3af46722c590c81adf30fc35738cfbcd992e3ca2ac29`.
The freeze binds the first three files below. Recorded filesystem modification
times are archival observations, not independent timestamp authentication.

| Original file | Observed source modification, 2026-11-12 UTC | SHA256 |
|---|---|---|
| `supplement-proposal.md` | 16:51:26.104299 | `6de45085b58b77cc98293149f814ed429a257161153c479813906251bdc79b71` |
| `supplement-proposal.json` | 16:51:26.104118 | `8bd54e97616e24dbc6ea3a259860d56f1181523910e762d347218d55490cd754` |
| `supplement-registry-s1.json` | 16:51:26.106770 | `c1594fbe4fef17261884dc30b6edbf593f4a388e919993c1d0bb3cf767e05e70` |
| `supplement-freeze-s1.json` | 16:51:26.110505 | `868ebc6e88dcbe3bfb1da229445d514970b3cf0b2ff82973fa31f5389a89222b` |

The exact hypotheses, clocks, features, predicates and baselines are in the
[proposal](../artifacts/research150-us/supplement-s1/supplement-proposal.md)
and [registry](../artifacts/research150-us/supplement-s1/supplement-registry-s1.json.gz).
This addendum summarizes their scope without replacing or retuning them.

## 3. Scope and full trial accounting

| Adaptation | Registered subject | Interpretation boundary |
|---|---|---|
| H079_US_S1 | Last two official monthly sessions plus momentum | Full calendar month, not the downloaded subset; fund orders and causality are unobserved. |
| H080_US_S1 | First two official monthly sessions | Long is the original positive-drift direction; the nine short policies are falsification controls. |
| H139_US_S1 | Fixed 16-return, three-scale R/S regime proxy | A tiny-window estimator, not proof of true Hurst persistence; both momentum and contrarian baselines are mandatory. |
| H140_US_S1 | Prior-only lag-one return correlation direction | Both baselines retained; 1m/5m variants adapt the source's explicit 15m wording. |
| H144_US_S1 | Signed ranked-tail balance from 60 prior native minutes | Fixed top/bottom-three statistic, not observed actors or a positive-versus-negative conditional tail estimate. |

The nine-symbol universe, 1/5/15-minute signal widths and independent holds,
separate directions, one-minute entry latency, 5/10/20 bps round-trip costs,
whole-session quality filter, cash sleeves and accounting conventions remain as
specified. Longer H139/H144 feature histories extend the actual information
intervals used for purge. Source ideas 71, 75, 87, 143 and 146 remain nonexecuted
in this supplement. All original-market ideas remain `NOT_TESTED`.

S1 registers **90 candidate/diagnostic policies**: 81 source-directed candidates
and 9 H080 short falsification controls. Its 126 fixed baseline entries produce
**216 total paths, 648 policy-cost views and 126 primary contrasts**. Multiple
fixed baselines cannot be selected after results. Similar baseline paths are not
independent replications.

Combined v2 + S1 accounting is **324 paths, 972 policy-cost views and 180 primary
contrasts**. This is a registered search count, not a number of independent
trials or tested source ideas. Combined inferential claims must cover the
combined selection family and preserve the larger historical search record.
The 60-day floor, full contiguous-block support requirements, all failed arms,
and partly observed chronological-period restrictions still apply. Relative
improvement through lower turnover is not evidence of positive net expectancy.

## 4. Implementation boundary and completion evidence

A pandas 3 timestamp-unit comparison issue was identified and corrected before
the first S1 run, using comparisons independent of underlying integer units.
The frozen feature definitions are unchanged. The final
[code hash record](../artifacts/research150-us/supplement-s1/supplement-code-hashes.json)
is dated `2026-11-12T17:02:49.007687+00:00`, SHA256
`a6d3e2079d6cf0abb2afe3ea4bb9759106a27a1b472acc748b116bb236901b0a`.
It records 13 synthetic checks passed in the repository environment and no S1
price-label read before its seal. Earlier tests in another Python environment
are not substituted for this final evidence.
The [validation proof](../artifacts/research150-us/supplement-s1/supplement-validation-proof.json)
retains the actual runtime, command and source identities.
All 13 checks also pass from a clean extraction of the archived v2/S1 source and
metadata plan, without opening market prices. The S1 archive manifest records
that portable-package verification separately from the original pre-run proof.

The [source ZIP](../artifacts/research150-us/supplement-s1/supplement-engine-source.zip)
contains exactly the four files in that final code mapping, preserving each
original byte sequence. Its SHA256 is
`c8256079bf1a24342c9af29063b9c13f7270720d12c3873bae66343621bbd491`.
It imports the seven immutable v2 files already archived separately. The
[S1 run seal](../artifacts/research150-us/supplement-s1/run-seal.json), SHA256
`eda6b774e23b4bbe8993a11d55d51c6d30c074a1766987c99d7f303f7d25e18d`,
binds the input manifest and v2 reference artifacts. Original market inputs and
v2 daily result matrices remain private prerequisites for a market replay.

The metadata-only [dataset plan](../artifacts/research150-us/coverage/dataset-plan.json)
is additionally archived for exact synthetic-test reproduction; it contains the
calendar and collection specification, no observed prices. This extends the
initial packaging description without changing the plan's frozen hash or
including raw data. The artifact README explains the combined directory layout.

Results must retain all registered candidates, diagnostics, baselines, costs
and failed or sparse contrasts, with first-pilot and S1 outcomes distinguished.
Do not call a favorable short H080 diagnostic evidence for the positive-drift
source claim. No runtime change, source reinterpretation, signal promotion or
broker execution follows from S1. This addendum does not require a winner.


## 5. Completed result

S1 measured every one of its 216 paths and 648 cost views. All 81 source-directed
candidates and nine short falsification controls are negative in development
at 5/10/20 bps. In the later period, two source-directed candidates are positive
at 5 bps and none at 10/20 bps; all controls and all 126 baseline entries remain
negative. Neither candidate/control zero-trade paths nor traded break-even
paths occur. Both positive H079 variants derive all their later-period trades
from October 30 alone. They are not independent calendar replications.

The [overview](../artifacts/research150-us/supplement-s1/supplement-overview.json),
[full cost-view records](../artifacts/research150-us/supplement-s1/per-policy-results.json.xz)
and [completion record](../artifacts/research150-us/supplement-s1/completion-evidence.json)
preserve the result. Joint inference includes all 180 current contrasts and
remains insufficient. The [consolidated result](phase-5.69-results.md) reports
both phases and the independent audit; no rule is promoted.
