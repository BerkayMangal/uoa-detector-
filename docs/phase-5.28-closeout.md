# Phase 5.28 — results and instrument costs

Implementation complete; publication remains blocked on GitHub API network access.

## Delivered

- Authenticated `/sonuclar`: full Live Alpha PAPER history, priced closed P&L,
  model-net mean, wins/losses/flats, separate pending/open/exit-pending/unfilled/
  unknown coverage, and policy/instrument breakdowns. Missing P&L stays unknown.
- Realised cumulative P&L maximum dollar decline uses exit order, starts at zero,
  and batches simultaneous exits. It is explicitly not portfolio NAV drawdown.
- Existing 1/3/5-session peer-relative lag scorer is displayed with coverage and
  profile versions. These are overlapping, pre-cost descriptive measurements
  from reference closes, not executable returns or a strategy verdict.
- Main page totals now cover all PAPER records rather than the last 20 displayed
  terminal rows. The last-20 detail slice stays, with its scope disclosed.
- Saved-plan comparison on actionable cards: stock position size/stop risk versus
  one option structure's entry debit, maximum loss, dollar/percentage round-trip
  cost, commission, expiry breakeven, affordable quantity and blockers.
  Withdrawal, missing quotes and risk blocks remain visible.

No new UW requests, schema migrations, strategy changes or production data writes.
No benchmark-relative PAPER return is invented: the stored PAPER record lacks a
matched SPY entry/exit quote. That requires a separate prospective capture change.
Manual fills and the separate Options Alpha PAPER engine are explicitly excluded.

## Validation

- Full hermetic suite: **4654 passed, 52 skipped**, 2 existing warnings.
- `mypy --strict src/ webapp/`, `ruff check .`, `uv lock --check`: passed.
- Nine new tests cover full history versus display bounds, unknown versus zero,
  separate outcome states, exit-ordered decline and simultaneous exits, policy/
  instrument grouping, peer/version coverage, authentication, HTML escaping,
  frozen option pricing/risk blocks, missing quotes and stale-entry withdrawal.
- After the final comparison wording/dollar-cost display change, reran the new
  results tests and the existing Live Alpha end-to-end job tests (38 passed),
  plus type/lint/lock checks.
- Real uvicorn startup with isolated SQLite: health 200, unauthenticated results
  401, authenticated results/home 200. A 30-close example with the oldest loss
  produced the correct -71 USD total on both pages.

## Decisions and remaining access

1. All history enters metrics; the detail table shows at most 200 newest records
   and discloses the bound. No records are removed or rewritten.
2. Unknown outcomes and unfunded/unfilled plans are excluded from realised P&L,
   not imputed as flat trades. Anomalous closed rows missing prices stay unknown.
3. Existing frozen peer scorer and costs are reused; no tuning or new edge claim.
4. Production health was reachable and returned SHA `88d3277`, with a PREMARKET
   `degraded` Live Alpha heartbeat. Its root returns 401; production Basic Auth
   credentials are not available in this machine, so the cause was not inspected.
5. Native Git access and push dry-run work. GitHub REST calls fail at CONNECT with
   proxy 403; this is not evidence that GitHub authentication is missing.
   Added `api.github.com` to the environment network draft, preserving the
   production hostname. Saving the draft is not runtime application/publication.
   PR creation, CI verification and merge/deployment require that access to work.

Next: create the PR after network settings apply, verify CI, merge, and verify
the deployed SHA and authenticated pages. Investigate the existing degraded
heartbeat with authorized production access separately from this reporting patch.
