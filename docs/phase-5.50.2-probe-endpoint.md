# Phase 5.50.2 — capability route correction

2026-11-01, before corrected probe. With the newly bound key, dated NFLX minute,
flow and gamma routes succeed. The fourth request is 404 because the new probe
incorrectly used /api/stock/{ticker}/historic. Existing verified repository
adapters use /api/stock/{ticker}/option-chains with date and greeks=true;
/historic belongs to the per-option-contract endpoint. Correct the probe and add
an explicit route regression test. Preserve old reports, including their actual
zero-request/blocked states. Old code hashes describe their original revision;
new measurements carry the new source hash. No profile or research threshold
changes, no claim that a 404 proved the account lacked option data.
