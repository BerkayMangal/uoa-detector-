# Phase 5.29 — personal desk and public introduction

Implementation and local validation complete; release status is tracked in the
feature PR. No strategy, calibration or upstream data changes.

## Delivered

- Shared charcoal/copper styling, Turkish shell, keyboard focus/skip link,
  scrollable mobile navigation and locally served 28 KB compiled CSS.
- Live cards foreground entry, stop, policy target, stop risk, chase limit and
  size. Evidence, opposing arguments, full cost comparisons, historical PAPER
  tables and technical explanations remain available in collapsed sections.
- Stock focus: case-insensitive search, hot list, measured peer-lag filter and
  explicit no-match state. The filter uses a stored boolean, not amber colour
  (which also describes leading names). Old snapshots without that flag do not
  invent a lag classification; a new worker cycle supplies it.
- Form edits and stock focus pause refresh without losing input. An independent
  browser timer withdraws entry presentation at the earlier of the existing
  freshness limit or session close, including early closes. PAPER-entry controls
  are disabled after expiry; immutable recommendations are not rewritten.
- Owner-authorized static `/hakkinda` is public. Its canonical URL, Turkish
  description/title/OG tags and SoftwareApplication markup describe the product
  without performance claims or private database access. Robots/sitemap expose
  only the introduction and required local assets. No search-engine submission
  or ranking is claimed.
- Exact GET-only public exceptions; private data, methods, documentation,
  unknown paths and asset traversal remain gated. Private/error responses are
  noindex/private-no-store, and health is no-store so release checks stay fresh.

## Evidence

- Full final gate: **4663 passed, 52 credential-gated skips, 2 existing warnings**;
  strict mypy (217 files), ruff, frozen lock check and diff whitespace check pass.
- Nine new test cases cover the public allowlist/method boundary, no private data
  access, host-independent canonical and truthful JSON-LD, robots/sitemap,
  private and error indexing/cache headers, lag versus leader flags, and entry
  lifetimes at stale-scan/session-close boundaries.
- Existing auth matrix retains all assertions, excluding only the explicitly
  authorized public GETs (371 existing auth cases passed).
- Chromium desktop (1440 px) and phone (390 px): no document overflow, no script
  errors, no external asset requests. Search/hot/lag/empty states and cost folds
  exercised. Clock advanced beyond expiry while editing a manual fill: input
  preserved, entry withdrawn, PAPER button disabled, ready count reset.
- Axe WCAG 2 A/AA and 2.1 AA scan: zero reported violations on `/` and `/hakkinda`.
  This is automated checking, not a claim of complete accessibility certification.
- Browser previews use labelled local fixture data only. Review images are at
  `/workspace/uoa-design-review/`; they are not production screenshots.

## Maintenance and limits

Runtime needs no Node or CSS CDN. To change styles, edit
`webapp/static/desk.css`, then run `bash scripts/build_ui_css.sh` from the repo
root (Node/npm required for development only). Tailwind 3.4.17 is pinned in that
script. Commit source and generated `uoa.css` together; increment the stylesheet
URL version in `base.html` when changing the cached asset in a later release.

No new predictive module is claimed: focus filters reuse existing UW-backed
hot-list/peer measurements. They improve navigation, not measured expectancy.
Multi-leg flow attribution remains a candidate for a separate data/cost review.

Release checks: green PR gate, merge to main, Railway success and matching
`/health` SHA; public SEO/assets 200 and private routes 401 without credentials.
Authenticated production-screen checks need the owner's access, not present in
this cloud machine. The existing degraded news heartbeat is a separate issue.
