# Phase 5.44 closeout — honest UI during database unavailability

KAPALI — authenticated requests with an uncaught database OperationalError get a
clear 503 rather than an ambiguous generic error. This improves outage behaviour;
it does NOT claim the production PostgreSQL capacity/connectivity incident is fixed.

## Behaviour

Private GET explains that current data cannot be verified and that signals/PAPER
results cannot be assumed current. Mutations explicitly have UNKNOWN outcome:
the handler cannot prove rollback or success after a failed database response.
No automatic retry, request replay, redirect or timed refresh is introduced.
The manual link is GET home, never resubmission of the financial form.

API failures return fixed JSON error/outcome labels with 503. Raw SQL, driver
text, parameters, credentials, paths and user input never appear. Existing auth,
private-no-store/noindex and public SEO remain. Non-OperationalError behaviour
and existing business/IntegrityError handlers are unchanged. A fixed fallback
also preserves uncertainty if template rendering itself fails.

Current production c39830d remains startup-failed at store.py:176 after the
serial-query release, with no completed new signal cycle. Latest connection
errors have no SQLSTATE; earlier 9ffe5b1 specifically reported **53100** capacity.
Do not assert a more precise physical resource from that alone. Railway volume
inspection/access was requested asynchronously; this environment has no Railway
token/database URL/production Basic credentials. No evidence was deleted or
capacity purchased. The prepared operator guide is docs/postgres-capacity-runbook.md.

## Verification and delivery

- **8 new cases** cover actual private GET, ambiguous HTML mutation, API read,
  API mutation outcome, auth/public pages, existing non-operational semantics,
  and both read/write template-failure fallbacks with secret-bearing SQL errors.
- Full hermetic gate: **5075 passed, 52 key-gated skips**; strict mypy **263 source
  files**, ruff/lock/diff clean. Existing assertions and all frozen artifacts,
  policies/profiles and forward_v2 source remain unchanged.
- Local actual app with a labelled synthetic failing DB: GET and REAL browser
  form POST at 1440/390px; correct 503/unknown outcome, no secret leakage, no
  automatic resubmission, overflow, JavaScript errors or axe WCAG A/AA issues.
  Existing styles reused; no asset build/cache change needed. No authenticated
  production visual inspection claimed.
- Commit Phase 5.44.1; green PR/main CI and matching Railway SHA required. Report
  the remaining external capacity/access blocker explicitly after deploy.

Next: actual Railway PostgreSQL capacity inspection/recovery, preserving recorded
evidence; then actual signal/desk/research cycle proof. HTTP 200 is not that proof.
