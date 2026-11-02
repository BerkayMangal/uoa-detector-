# Phase 5.52 — native one-shot operator transport

2026-11-02, before native execution. The cloud executor's direct DNS is unavailable
for all three tested domains (UW, Railway proxy and app), while its approved HTTPS
proxy works. Scoped native metadata confirms the existing PostgreSQL proxy still
matches application configuration. Do not treat this executor transport limit as
a bad password, change the database URL, bypass network controls or ask for keys.

Keep the original preview/apply CLI. Provide the same bounded repair in the
application environment through an explicit non-secret DAILY_REPAIR_REQUEST id.
Empty means disabled. An atomic append-only claim for that exact id wins before
any HTTP call; another process/redeploy cannot repeat it. Crash/claimed-without-
result means incomplete, never an automatic retry. A new operator id is required
for any further authorized attempt. This is a single startup task on its own
service thread, not a recurring loop or an HTTP-triggered write.

Use a new private alfa_daily_repair_run table only for claimed/source/result
receipts. Preserve all existing prices/cohorts/outcomes/job markers. Persist
compressed sealed source packets and the small result manifest there before the
operator finishes; source data stays authenticated/private outside git, never in
health or public responses. No credential or connection URL enters packets.
A private SELECT-only maintenance endpoint may show safe result counts/status,
not raw payloads, SQL or secrets. Creation and writes happen only in the native
operator; normal requests neither create tables nor mutate records.

Deploy checked code with the operator disabled, then activate exactly one scoped
request id through the existing Railway API. Verify actual completion/coverage,
physical receipts, request counts and first-write ownership; keep unknown/partial
results visible. Operator failure is isolated from web liveness. This changes
transport within the owner's authorized data-repair task, not its budget,
strategy thresholds, historical availability or promotion rules.
