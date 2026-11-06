# Phase5.62.1 — inspect source size before an explicit bounded export

Engineering addendum registered before code,2026-11-06. Both actual completed
November4/5 exports refused with source_limit_exceeded under5.62. No partial
dataset, outcome or fitted trial was produced; the native application remains
healthy. Do not edit5.62's original contract or relabel its refusal as a backtest.

Add a metadata-only operator request returning the already computed source
scan/character counts, bounds and limits without materializing snapshots.
Preserve existing payload/default16MiB behavior. Permit an explicit per-request
source-character budget between1 and64MiB only after that day's metadata is
reviewed. Keep256 scans,8MiB response, frozen10000 adapter bounds, one concurrent
export and all auth/origin/complete-day/source safeguards. Reject requests above
the hard budget or a source exceeding it; no automatic increase, partial export,
new vendor request, profile/strategy change or research promotion.

The live operator will inspect November4/5 once, then request the smallest whole
MiB covering a day only when <=64MiB. Record actual counts, requested budget and
parent identities in the pilot evidence. If hard limits still prevent export,
state blocked coverage rather than synthesize a result. Other input, method and
80-session inference constraints remain unchanged. Test metadata-only SELECTs,
hard-budget refusal, default behavior and actual explicit-budget export; run
the full gate/exact CI/native publication workflow again.
