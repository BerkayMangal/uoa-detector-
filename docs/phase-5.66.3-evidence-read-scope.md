# Phase5.66.3 — bounded evidence reads within one observation

Registered2026-11-07 before implementation. Phase5.66 native verification returned
HTTP200 with the original four candidates/all12 references unchanged, but took
108.3348seconds. Local profile parsing improved without solving627 remote SELECT
roundtrips. UOA's active database is in the owner's separate merry-consideration
Railway project and uses a public TCP proxy; both deployments specifyus-west2.
Neither database in UOA's own project matches that endpoint/password. Do not
switch to an unrelated same-named private database, move data or alter credentials.

Introduce an operational, bounded, observation-local SELECT result cache in a
new module outside frozen source/storage/verifier identities. Use SQLAlchemy's
ORM execution hook/FrozenResult merge pattern under an explicit ContextVar scope;
include the actual engine/bind, complete statement structure and exact bound
parameter values in cache identity. No global method monkeypatch, cross-request
cache, TTL, incomplete-key cache or bypass of a verifier. The original query's
full immutable source rows and physical availability checks still feed the same
original functions. Cache hits do not omit source/hash/clock checks.

Enable only pure read-model observations and the new fusion/semantic/fundamental
read portions; never hold a cache scope across provider calls, commits, worker
polls, or distinct decision times. A new observation must see new receipts.
Unexpected writes in a scope invalidate/disable reuse rather than expose stale
rows. Uncacheable/nonfinite/ambiguous parameters and over-budget entries fall
back to ordinary reads. Bound keys, retained rows and scope lifetime; release
all retained results on exit including exceptions. Keep unrelated ORM work
outside the scope unchanged and isolate concurrent tasks/threads/engines.

Preserve all old profiles, source/target/capture identities, frozen research
artifacts, historical outcomes and original source modules. New worker identity
changes may create a new independent activation at the actual deployment time;
there are currently zero eligible new fusion candidates. Never backdate that
activation or reinterpret the old November6 cohort as new decisions.

Measure actual SQL count and complete-report parity on the nonempty four-name
fixture. Test bound parameters, engines, clock changes, missing/newly committed
receipts, budgets, exceptions, concurrent scopes, no-scope behavior and readonly
API authentication. Run the unchanged full gate before a commit, exact-green
feature/main CI and native exactSHA/SUCCESS. Measure the same November6 full
report after deployment, preserve all four rows/12pairs and source identities,
and confirm new workers/pages/old82priced results before claiming completion.
No API/model request, strategy threshold, statistical gate or promotion changes.
