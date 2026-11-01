# Phase 5.51 — fixed, shared within-day timing controls

Registered before inspecting diagnostic returns, 2026-11-01. For the three
fixed base-cost arms, compare their actual selected ticker/session sets with
1,000 seeded random-entry draws. Draw from the same registered intraday bucket
schedule within each selected ticker/session, with the same delay, horizon,
whole-unit budget, reference and costs. Generate one ticker/session timing map
per draw shared across arms, preserving cross-arm dependence. Missing exact
marks invalidate that arm/draw; do not resample until it becomes favorable.

Report actual means, the descriptive fraction of valid null scores below the
actual score, null ranges and invalid-draw counts. These are 3,000 resampling
calculations, not 3,000 independent strategies/trades or fresh p-values. They do
not change the fixed twelve replays, the 80-session selection gate, or the null
winner. No promotion or retuning based on this diagnostic. Native-time and
quote-quality limitations remain unchanged.
