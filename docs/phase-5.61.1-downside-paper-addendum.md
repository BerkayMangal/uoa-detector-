# Phase5.61.1 — downside source PAPER capture

Registered before this additional code change. During the owner-authorized
downside audit, automatic source PAPER creation was found to select only BUY
recommendations, excluding existing READY BEARISH_SETUP/P4 plans. Existing PAPER
tracking already supports downside stop/target and PUT mark/exit pricing.

Extend only the future simulation selector to READY downside recommendations
with direction down and preferred long_put/bear_put_debit, an existing READY
priced positive-lot structure and stock-reference plan. Fail closed on any
short-stock or absent/unpriced option substitution. Keep one PAPER per source
opportunity and existing real-quote freshness, cost, risk and later-fill guards.
Tag new downside plan JSON auto_paper_scope=downside_source_v1; unchanged upward
plans keep their old payload. Do not rewrite past records or repeat old trades.

This is the existing SOURCE simulation lane, not opening-first-call timing, a
new 15/30/60-minute option-return experiment, calibrated alpha or a real order.
Keep policy/profile thresholds and frozen study identities intact. New downside
PAPER results remain separable by original instrument and explicit source tag.
Verify actual directional source/priced views→record→PAPER_PENDING, restart
deduplication, excluded/unpriced/mismatched/disabled paths and existing tracker
regressions, followed by the full gate and exact native publication checks.
