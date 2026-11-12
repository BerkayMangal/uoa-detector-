# Phase 5.70.1 — Correct the ATM acquisition receipt clock

Frozen before this repair, 12 November 2026, within the owner's request to fix
automatic data collection. Inspection found that `refresh_atm_chains` assigns
the batch-start time to every ticker's later response; `run_atm_cycle` can
reuse an even earlier clock sampled before expiry-list requests. Consumers
interpret this field as acquisition time. A later successfully received
response can consequently appear older than it is.

Repair the production refresher to supply an injected completion clock, sampled
after each successfully decoded ticker response. Store that receipt time only
for the response's rows. Preserve provider trade dates and leg observation
timestamps, existing request bounds and stale-data rejection. Receipt time is
not exchange quote time or proof that the vendor's underlying price is live.
Do not change freshness thresholds, loop cadence, selection rules, prior rows
or research verdicts. Keep deterministic historical callers compatible.

Tests must simulate sequential delayed responses and an expiry-list delay,
verify per-response receipt identity and untouched vendor timestamps, and
show failed/no-data calls do not refresh an earlier stored quote. Run the full
repository gate with Phase 5.70 before commit. This repair alone does not claim
to explain all missing signals or make any plan eligible.
