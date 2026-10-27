# Phase 5.28 — PAPER results and instrument costs

Approved by Berkay in chat on 2026-10-27: "dediklerine katiliyorum ve basla".
Frozen before implementation. This is a reporting change, not a strategy study.

1. Add an authenticated `/sonuclar` page that reads existing Live Alpha PAPER records.
   Report all records, separating pending, open, exit-awaiting-price, unfilled,
   unresolved and priced closed states. Never treat unknown outcomes as zero P&L.
2. Totals use the full history, not the home page's last-20 display slice. Show
   policy/instrument breakdowns, priced wins/losses/flats, average net model P&L,
   and maximum dollar decline of the cumulative realised P&L series. Label that
   series as realised only, not portfolio NAV or mark-to-market drawdown. Group
   simultaneous exits before calculating the decline. No annualisation or edge claim.
3. Read the existing lag records and frozen forward scorer for 1/3/5-session
   outcomes; show coverage and descriptive averages for all/lagging/leading
   records by profile version. These are peer-relative percentage points before
   trading costs, not trades or causal evidence. Missing tables/data remain unknown.
4. Add a visible stock/option cost comparison to live actionable cards using
   their saved stock plan and option structures, without repricing or API calls.
   State units: stock position versus one option structure; quantities from the
   same saved risk budget. Distinguish planned stop risk from option maximum loss.
   Expiry breakeven is not the stock plan's five-session forecast. Do not claim
   stock costs are known when absent from the saved plan.
5. No existing record edits, schema migrations, threshold changes, additional UW
   calls, or broker execution. Preserve frozen profiles and old acceptance docs.
6. Meaningful regression tests must cover >20 records, unknown outcomes,
   policy separation, exit ordering, missing lag coverage, quote/risk blockers,
   authenticated HTTP rendering and HTML escaping. Run the repository gate.
7. Push a feature branch and use the PR workflow. The owner's current instruction
   authorizes handling push/deploy; merge only after the checks are green.
   Verify production SHA and pages when access is available; otherwise report
   the precise publication/access blocker without claiming deployment succeeded.
