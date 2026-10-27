# Phase 5.44 — clear product behaviour during database unavailability

Frozen before implementation, 2026-10-28 Istanbul. After the bounded-memory c39830d
release, production still reports startup OperationalError at store.py:176, with
no first completed scan. Previously verified 53100 remains the known incident;
latest connection errors lack SQLSTATE, so don't invent a more specific volume
or memory diagnosis. A Railway operator resource check is pending; this cloud
workspace has no infrastructure credential. No paid action or destructive data
operation is authorized by this UI change.

For uncaught SQLAlchemy OperationalError on an authenticated product request:

- HTML GET: status 503, concise Turkish explanation that database access/current
  signal/PAPER measurement is unavailable; no successful/recent-result claim.
  Offer a manual GET link to home. Keep personal data behind existing Basic auth.
- HTML mutation: status 503 and explicitly UNKNOWN outcome; never claim the
  transaction rolled back, failed permanently, or succeeded. Tell the owner to
  check recorded transactions after access returns before re-submitting. No
  automatic request replay, background retry or timed refresh.
- /api routes: status 503, fixed JSON error/outcome labels; no raw exception,
  SQL, params, host path, server code, credentials or user input. Unknown outcome
  for mutation methods, unavailable read for GET/HEAD.

Preserve private no-store/noindex behaviour. No changes to healthy routes,
transactions, scoring/profiles, research, paper records or quota. Do not catch
IntegrityError/validation/business errors as outages. Health remains a separate
safe operational endpoint; public landing/SEO do not become private data views.

Reuse existing styles, concise accessible markup. Test real private HTML GET,
API JSON and mutation ambiguity, existing auth and secrecy of secret-bearing
driver messages. Full hermetic pytest, strict mypy src/webapp, ruff/lock/diff;
local desktop/mobile overflow/accessibility/JS checks with a labelled synthetic
database-failure fixture. Green PR/main CI and matching Railway SHA. Report
remaining actual database capacity/access blocker honestly, with the prepared
operator runbook, rather than claiming pipeline recovery.
