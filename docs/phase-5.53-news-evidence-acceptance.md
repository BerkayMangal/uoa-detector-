# Phase 5.53 — physically observed news and fixed policy ablations

Frozen before implementation, 2026-11-04. Owner supplies a research design brief
and authorizes making the system robust, including necessary news APIs and the
existing test/PR/merge/deploy workflow. The attachment is reviewed source material,
not permission to unseal Study G, buy a feed, send vendor data to an external LLM,
change the live strategy or represent assumptions as measured performance.

## Scope and immutable boundaries

Keep all live profiles, thresholds, original recommendations, PAPER records,
opening/event-calibration/alpha-lab frozen implementations and historical artifacts
unchanged. Do not open reserved Study G prices/labels. This is a separate research
version, no trading execution or automatic winner promotion. Preserve prior
negative results and the existing 438-candidate/80-session support gate.

Existing UW headlines have application-cycle timestamps, not post-response physical
receipts. New recording wraps only already-authorized /api/news/headlines calls:
zero additional vendor calls, same quota reservation/cadence and original return.
Store each successful raw response privately with its hash, request/receipt clocks,
ticker and a separate receipt timestamp after the packet transaction commits.
A failed research write cannot change the live response or retry the API. Missing
receipt, failed check, malformed/future/revised/mixed data remain unknown. No
retroactive assignment of strict availability to old news rows.

New append-only news tables are initialized at worker startup and registered for
durability. Add machine-readable PriceContext evidence to new scan snapshots;
finished-scan time is the earliest research decision time. Historical scans lacking
this feature block are not reverse-engineered from explanatory text. Original
live decision timestamps are never rewritten.

## Fixed experiment, not a fitted optimum

A common base roster: first eligible long flow+price opportunity per ticker/ET
session after open+15, chronological fixed four slots/day before auxiliary gates.
Qualifying source flow/price thresholds are copied by reference from the frozen
live profile; current price and exact prior close/source session are required.
No result-dependent replacement or reallocation after a gate removes a slot.
Five frozen arms: A base; B base+RS; C base+news; D base+RS+news; E base+(RS OR news).
A/B/C/D form the factorial; E is a distinct OR-policy comparator. This prospective
roster is not asserted to replicate every live publication or a cash portfolio.

Primary outcome: one-minute delayed, 60-minute held, long-stock independent
USD2,500 capped whole-share reference units; first strictly later observed price
within five minutes, paired SPY at exactly the same source times. Costs 5bp per
side; stress 10/20/50bp is diagnostic only, never another selection opportunity.
No short sale, intraday stop sequencing, option fill or EOD-option P&L fabrication.
All selected outcomes must be present for an arm/day; incomplete days stay unknown,
never zero. Ineligible known gates allocate zero to the same preassigned slot;
unknown gates do not become false. Use common complete days for paired daily
contrasts, primary E-minus-B, and report N-and-not-R opportunities separately.
Do not compare conditional means of different selected samples as a causal effect.

Strict news flag uses successful physically receipted packets at or before research
decision time, source publication within 36h, exact ticker association, no future
publication timestamps and duplicate-headline handling. Checked-empty is distinct
from unavailable. Presence is not positive sentiment or an economic surprise.
Reuse no future-trained model as a historically causal classifier. Directional
news, earnings, normalized flow, sector/beta and time-placebo tests are separately
registered follow-ups, not uncounted searches in this fixed presence experiment.

Report source/profile/protocol hashes, roster/receipt/outcome coverage, every arm,
all missing reasons, paired daily net P&L/excess, concentration and uncertainty.
No winner or success probability. Fewer than 80 complete common sessions blocks
inferential readiness; pilot ledgers/counts remain descriptive. The old research
period and reviewed forward history cannot become fresh untouched out-of-sample.
Do not annually scale Sharpe by trade count; do not infer beta-neutral alpha from
SPY subtraction. A null marginal H1 does not logically eliminate conditional
hypotheses; missing evidence and inconclusive tests are not proof of zero effect.

## Delivery and verification

Typed recording/read-only research adapters, fixed ablation evaluator, offline CLI,
private research-page readiness report, corrected research design and actual
coverage evidence. Read-only GET does not create tables, fetch vendors, optimize,
write records or execute research loops. Bound query/export sizes and disclose
truncation; do not compute partial-universe performance as complete evidence.
Meaningful tests cover post-commit availability, unknown/future/news leakage,
boolean truth tables, stable roster, common missing labels, costs and paired
exposure. Preserve old test assertions. Full hermetic gate before commits,
green PR/main CI, exact native deployment SHA and real news-recording/readiness
verification before declaring live. No paid/new external feed required unless
actual capability gaps justify a separately configured provider.
