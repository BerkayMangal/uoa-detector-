# Phase 5.69 — US intraday research closeout

**Research measured and archived; no validated edge or live promotion.** The
owner requested autonomous US research on the supplied 150 ideas. Eight
explicit US adaptations were evaluated at 1/5/15-minute signal and holding
intervals. The original BIST/VIOP claims were not tested.

The [result record](phase-5.69-results.md) and
[S1 addendum](phase-5.69-S1-addendum-record.md) retain every registered arm,
timing disclosure and source distinction. The first pilot was frozen before
its outcomes; S1 was specified separately after v2 execution started, with a
feature author who had not inspected v2 outcomes and a runner author who had.
Neither run is fresh confirmation.

## Measured result

The combined registry contains 135 source-directed candidates, nine H80 short
falsification controls and 180 baseline entries: 324 policies, 972 cost views
and 180 primary paired contrasts. These are dependent alternatives.

| Period | Complete common dates | Positive source-directed candidates at 5 bps | At 10 bps | At 20 bps |
|---|---:|---:|---:|---:|
| January–September | 103 / 187 | 0 / 135 | 0 / 135 | 0 / 135 |
| October–November 11 | 19 / 30 | 3 / 135 | 0 / 135 | 0 / 135 |

All nine falsification controls and all baseline entries lose at every cost
in both periods. Two of the three positive later-period candidate rows use
the same October 30 calendar event; repeated intraday trades do not create
independent event replications. None survives the 10 bps stress. Costs are
assumed stock round-trip frictions, with unverified borrow; these are not
option returns, broker fills or actual account performance.

All 1,953 planned ticker-days were attempted with 1,730 acquisition GETs.
The frozen quality rule certified 1,824 ticker-days and 711,360 minute rows.
All 129 excluded ticker-days still contain complete native price timestamps;
146 negative minute-volume observations caused their exclusion. The common
sample is retrospectively conditioned on full-session quality. Joint
inference and DSR remain insufficient; no missing day is treated as cash.

## Validation and delivery

- Full hermetic repository gate: **7,257 passed, 52 skipped**, two warnings,
  426.22 seconds. Strict mypy passed on 399 files; Ruff and lockfile checks
  passed. Application source and signal profiles are unchanged by this phase.
- Frozen v2 source: 20 synthetic tests. S1: 13 synthetic tests, including
  the complete 216-policy path exercise. Clean archived-source extractions
  passed. The pandas timestamp-unit defect was fixed before S1 outcome access.
- Independent quantitative audit: **11,964 checks**, zero failures, covering
  1,944 policy/period/cost summary rows, source hashes, daily arithmetic,
  cost decompositions and inference gates.
- The undeployed news-export prototype passes 13 synthetic checks and reduces
  repeated reads in its synthetic fixture. HTTP/PostgreSQL, total runtime and
  response/decompression limits remain implementation requirements. This phase
  does not claim that the production export bottleneck has been fixed.

Judgments: retain the frozen quality rule after observing volume corrections;
keep both H139/H140 baselines; classify H80 short paths as falsification controls;
preserve all outcomes rather than select positive rows; keep raw licensed
prices and news out of the compact review archive. Study G inventory exposure
is disclosed in the result record: raw CSV bytes were fully parsed, but only
schema/coverage metadata was analyzed; its prescribed experiment was not run.

The shipped ThetaData connection belongs to PRs #148/#149. PR #149 is live at
`807a3079a7b53620db0e1de8a1afa8777714b417`; deployment and authentication checks
passed. At the last check during this research, the Mac connector had not
contacted UOA and eight option-validation requests were waiting. This research
does not turn that external connection into verified ThetaData ingestion.

Next: obtain the first real Terminal packets for the fixed option requests,
then measure quote coverage and bid/ask economics. Any later price/news/option
predictive test needs its own exact specification and prospective evidence.
No order execution, broker connection or automatic signal promotion is added.
