# Phase5.63 — automatic daily fixed-news accountability

Implementation verified; exact release/CI/native evidence is appended to the PR
after publication. No validated alpha or fitted probability is claimed.

## Actual calculations and resulting behavior

The original November4/5 frozen experiment ran on286 scans/3651 candidate rows.
Its8 fixed roster slots have0 fully priced units:3 entry-missing/5 exit-missing.
All-five common sessions0; E/B paired days0. The40 daily arm/cost summaries reuse
two sessions and are not40 independent strategies. Unknown returns remain null.
No historical call, PAPER, quote or outcome was repaired to create a result.

Two checksummed aggregate seeds retain original source identities, profile/code,
day bounds and export/report/audit hashes. Newly imported summaries receive an
actual recording clock in the separate append-only pilot namespace. The source
clock is never replaced by the import time. Full raw exports remain private.

The isolated worker imports those two recorded calculations once, then evaluates
the latest completed ET session once per method/profile/code/day. Durable claims
precede work, concurrent work is bounded and unfinished/failed claims are not
automatically replayed. Limits and the frozen five-arm evaluator remain intact;
there is no vendor I/O or strategy/threshold promotion. PostgreSQL has a separate
two-connection pool, statement/lock timeouts and disabled parallel query workers.

Existing private `/arastirma` now includes the small stored daily report;
`/api/arastirma/pilot` reads the same bounded dataclass view. All five arms, all
four fixed cost scenarios, unknowns, explicit cash-zero, failed/pending days,
source identity, clock and display bounds remain distinguishable. No new menu
item or alert controller was added. GET neither creates storage nor evaluates.

## Verification

- **6257 passed,52 key-gated skips,two existing warnings**,154.33s; strict
  mypy341 files,Ruff,lock and whitespace checks clean.
-46 focused core/view/integration cases, including actual registered-source
  adapter/evaluator, actual aggregate seed, signature/hash/profile/day guards,
  DST/weekends, post-work recording clocks, concurrency/restart and private
  SELECT-only API. Generic route authentication adds15 cases automatically.
- Both new tables registered as append-only in the unchanged durability guard.
  New job tests isolate process status and opt in explicitly; ordinary app
  lifespan tests keep background evaluation disabled. Existing assertions kept.
- Chromium1440/390/320 passes for the complete research page, with15 navigation
  links and no horizontal page overflow even after opening every new disclosure.
- Independent offline arithmetic/state reconciliation passes64 missing reference
  valuations/320 slot states/80 day totals across repeated source/cost views.
  There were0 priced valuations; this is not independent roster/news/mark-choice
  reconstruction or proof of return performance.

## Material limit

The five-minute scan cadence plus source/completion lag can miss the frozen
entry/exit observation windows. Daily automation exposes that gap; it cannot
recover old prices or establish the marginal benefit of news from two sessions.
Current timed capture follows published calls and is a separate method. A new
prospective pre-filter shadow capture protocol would be required to solve that
additional coverage problem without changing the frozen experiment or blessing
later prices. Original80-session gates and all prior negative verdicts remain.
