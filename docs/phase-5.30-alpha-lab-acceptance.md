# Phase 5.30 — observable flow and prospective alpha laboratory

Frozen before code, 2026-10-27. Owner approves the preceding proposal in chat:
"hepsine katliyorum sirayla basla yap bitir". Engineering delivery, not an edge claim.

## 1. Ordered delivery

1. Archive observed UW flow-alert payloads before lossy legacy conversion. Preserve
   first-seen availability, provider timestamps, side premiums, multi-leg flags and
   revisions in NEW append-only `alfa_lab_*` tables. No existing table/model/profile
   alterations, deletions, historical reconstruction or extra vendor requests.
2. Shadow flow arithmetic allocates actual ask/bid premiums, not the whole alert
   to its majority side. Unknown flags, malformed partitions, multi-leg alerts,
   unavailable/future data and conflicting revisions are disclosed and excluded
   from directional evidence. Alert identifiers deduplicate observations, not
   underlying trades; overlapping vendor aggregates cannot be claimed independent.
3. A prospective peer-recovery cohort records first common eligibility per
   (policy, ET session, ticker): measured prior-close peer lag, positive peer move,
   fresh same-session price recovery. Treated versus control differs only in
   corrected recent flow qualification. Both need observed raw alerts so missing
   capture is NOT a no-flow control. Unknown flow coverage is excluded/disclosed.
4. News audit reuses existing checked headlines: separate positive, negative,
   mixed and unclassified observations, normalized syndicated text and evidence
   availability. Rule-based headline classification is not an earnings surprise,
   source verification, article comprehension or a confidence probability. It is
   recorded as a covariate, NOT a baseline filter or an automatic trade trigger.

## 2. Frozen pilot (alpha_lab_v1)

Thresholds live in a new profile; existing live_alpha_v1 stays byte-identical.
Recent flow window 15 min; inherit minimum directional premium $250k, dominance
65%, known-side share 50%, two distinct contracts from the existing policy as
design choices, NOT fitted optima. Peers and lag cutoffs use the existing versioned
basket/peer definitions. Recovery minimum 0.1 ATR; maximum 1 ATR to avoid chase.
Capture must include a successful poll within 120 s, and data anomalies/ambiguous
multi-leg coverage cannot silently become a clean control.

Primary result: three sessions. Secondary: one and five, not alternate winners.
Unit: first common-eligible reading per ticker/session; later arm changes do not
rewrite it. Entry: NEXT declared regular session OPEN; exit: close of its Nth
session (entry session is 1). Freeze peer membership and input hash at observation.
Price both stock and matched SPY/peer reference portfolios over the identical
entry/exit dates. Model 5 bps adverse execution each side; commissions, taxes and
borrow costs not modelled. These are descriptive counterfactual stock returns,
not executed hedges, options trades or verified fills. Missing any required
open/close makes the comparison unknown, never zero or an earlier exit.

Every candidate, including exclusions, is audited; the cohort samples only this
observed UW-selected universe. It cannot estimate the full-market effect or
identify causality. Results overlap and share dates; a positive mean is not proof.
No significance/pass/alpha label or live-policy promotion is automated. A formal
power/stability/multiplicity assessment needs a separately frozen analysis plan.
Previously burned windows and rejected studies are not retuned or rescued.

## 3. Operation and verification

Raw capture is batched, async/off-loop and idempotent across restarts. A capture or
lab failure is visible and cannot stop legacy ingestion or convert missing to zero.
The supervised Live Alpha loop runs the lab AFTER its legacy cycle; research
decisions and outcomes cannot feed back into BUY, sizing, PAPER or ranking.
Only additive table creation. Profile/data-source versions and UTC times retained.

Private `/laboratuvar` shows raw coverage, corrected flow, cohort arms/outcome
coverage, headline evidence and job status. Keep the new concise UI; no private
data in public SEO/health (health may report status/counts/timestamps only).

Required tests: source-to-archive-to-shadow path, 510k/490k counterexample,
puts/multi-leg/unknowns, replay/revisions, as-of/future guards, no-data controls,
frozen arms/peers, news negation/conflicts/syndication, next-session entry and
1/3/5 matching outcomes, missing prices/early close/calendar gaps, restart/failure
isolation, private route/auth, end-to-end supervisor wiring, no extra API calls.
Run full repo gate, PR CI, merge and verify Railway SHA and gated route.

Engineering can finish now. Economic validation requires future observations and
complete outcomes; that waiting period is not a credential or approval substitute.
