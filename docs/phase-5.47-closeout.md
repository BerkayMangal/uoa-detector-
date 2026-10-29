# Phase 5.47 closeout — deterministic runtime and outage isolation

Implementation/test complete. Actual delivery and PostgreSQL recovery are
separate gates. Owner log confirms native recovery mode, SSL EOF, 500 logs/sec
and dropped messages; no database target/TLS/WAL/data changes are performed.

## Resulting runtime

Explicit Railway Docker builder replaces autodetection. Python 3.12.15 slim and
uv 0.11.12 are pinned by image digest; locked main-only dependencies and a
non-editable project wheel are installed. Required source/profiles/artifacts stay
at their existing paths. .dockerignore excludes environment/keys/git/host caches/
local DBs/logs/raw harvests. Existing PORT/start/health/restart settings remain.
An optional build-only trusted-CA secret supports the cloud workspace's configured
HTTPS proxy; it is not copied or stored in runtime ENV. TLS remains verified.

For PostgreSQL, ingestion/gamma/refresher live for their entire lifetimes on
separate owned daemon threads/async loops. HTTP no longer waits on their native
startup I/O. Each client/session remains on its owning thread; return/error is
propagated after cleanup; cancellation reaches the owned task without joining a
blocked native call or creating another attempt. Original supervision/backoff
stays intact. SQLite and existing live-alpha/prerender paths remain compatible.
/health has a thin async memory-only HTTP wrapper while its direct Python helper,
JSON schema, name and operation ID remain intact. Liveness never implies data.

Known storage restart errors become one safe log record with class/category/
relative site/SQLSTATE/resource/allowlisted connection cause. Native messages,
SQL/parameters/URL and repeated tracebacks are omitted. Unexpected programming/
vendor exceptions retain traces. Existing durable failure states/evidence remain;
no transaction replay, vendor calls, quota changes or policy adjustments.

Parallel boot exposed an existing global ruamel parser shared by profile loads:
valid profiles produced IndexError/MappingEndEvent/ParserError in the actual
Docker test. Each read now owns its safe YAML parser. Serial vs concurrent profile
values and content hashes match exactly; no YAML profile or strategy value changed.

## Verification and judgment calls

- 27 new cases: full loop/thread/context ownership, PostgreSQL schemes, SQLite
  affinity, normal/error completion/cleanup, cancellation before start and during
  a native wait, child/async-generator cleanup, real app lifespan HTTP with three
  blocked jobs, exhausted sync-request-pool liveness, compact/secret-free SQL
  errors, recovery+SSL EOF, preserved unexpected traces/backoff, parallel profile
  parsing with identical values/hashes.
- Controlled pre-fix GammaRepo wait blocked a scheduled 20ms web-loop callback
  until ~266ms; no real DB/vendor calls. New actual Docker health was 1–13ms
  while all four live services reported connection refusal to an isolated local
  closed port; private opening returned 503/BEKLE/clock/private-no-store/no scripts,
  not a fake entry. All four primary storage restart paths emit compact lines;
  profile parser races disappeared after the per-read parser fix.
- Actual final Docker build/start, live profile validation, health SHA, public
  intro/CSS, private auth and outage screen verified. Network isolated at runtime,
  dummy credentials only; no real API/production DB. Image/runtime do not contain
  .env/.git/build CA/proxy credentials. Three frozen calibration/tournament/stress
  report integrity checks succeed inside the image. All earlier modules included.
- Full gate: **5300 passed, 52 key-gated skips**, strict mypy **275 files**, ruff/
  lock/diff clean. Trading profiles, frozen research runtimes/reports, assertions
  and results remain unchanged. No source optimizations/backfills or invented alpha.

One test-fixture correction is documented under D10: two old research-roster
scenarios used 2026-10-28 but read the machine's later physical clock. On October
29 both also fail in a pristine archive of unchanged main 5d74a1b (2 failed/4
passed). Inject the scenario's declared clock in that module's existing fixture;
explicit late-publication clocks remain untouched. All assertions and production
publication_time/runtime hashes are unchanged; no backdating rule is weakened.

Local image building needed the execution workspace's proxy host mapping and
trusted public CA bundle via a temporary BuildKit secret. Neither the mapping,
proxy nor certificate is embedded in Dockerfile/runtime image/Railway variables;
standard provider networking uses its ordinary verified TLS path. This validates
the image, not the earlier builder's unobserved cause or native DB recovery.

Release: Phase 5.47.1, green PR/main CI and actual matching Railway SHA required.
After publish verify actual cycles, not merely HTTP200. Native PostgreSQL recovery
can remain unavailable independently of a correctly serving/released web app.
No further request for the same owner log, no database reset or paid resize.
