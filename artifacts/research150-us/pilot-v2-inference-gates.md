# Additional pre-outcome numerical gates

2026-11-12, agreed with the engine owner before any pilot result is evaluated.
This adds explicit failure criteria to the existing frozen v2 inference method;
it changes no strategy, signal, cost, clock or data selection.

- Retain the minimum60 complete paired market sessions.
- A contrast needs at least20 nonzero daily candidate-minus-baseline differences
  plus positive finite Bartlett HAC variance. Otherwise its interval/statistic
  is INSUFFICIENT_EVIDENCE. Do not impose a denominator floor.
- For each prespecified block length separately, require at least20 distinct
  eligible contiguous block starts, and require every analyzed session to
  appear in at least one eligible block. If not, that period/block-length
  inference is INSUFFICIENT_EVIDENCE. Do not estimate a full-period mean by
  resampling only a small supported fragment of its dates.
- Blocks must be contiguous in the official trading-session calendar; weekends
  and exchange holidays do not constitute missing sessions. Missing expected
  trading sessions do break a run. Five-session primary and ten-session
  sensitivity retain their original roles.
- Report all54 registered contrasts, including excluded degenerate/sparse
  columns with reason and count. A max-statistic calculation on eligible
  columns does not make the others pass and does not erase them from the search
  ledger. These are conservative numerical governance floors, not guarantees
  of adequate power or an untouched-sample interpretation.

Canonical108-policy registry IDs remain authoritative. If an implementation
uses different internal names, outputs must retain an explicit one-to-one
canonical_trial_id mapping for each of the324 policy-cost views.
