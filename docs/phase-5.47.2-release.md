# Phase 5.47.2 release — portable Docker selection/install

2026-10-29 Istanbul. All earlier features remain included. Owner explicitly
requests Codex handle the release through the existing GitHub connection, with
no further panel task. Previous public runtime cdc7add and latest candidate
1490313 differ; no prior successful current-main delivery is claimed.

Use the documented Dockerfile path without the optional builder selector.
Remove the anonymous BuildKit dependency cache mount, which does not match
Railway's documented service-prefixed cache ID format; uv installs the same
locked runtime packages without a package cache. Keep verified Python/uv image
digests, build-only trusted-CA support and TLS, runtime contents and deploy
start/health/restart settings. No database reset, purchase, secret, vendor work,
policy/profile/research threshold or application behavior change.

Verification at the final Docker/code state:

- 5300 hermetic tests passed, 52 credential-gated integration skips; strict mypy
  clean on 275 files, ruff and lock checks clean. No assertions/tests changed.
- New actual image built successfully, then started with network disabled,
  local SQLite and temporary in-memory auth credentials. Lifespan/health 200
  identifies the test revision, public page/latest CSS 200, private surfaces
  reject unauthenticated requests, authenticated opening 200/private-no-store.
- pandas/pyarrow/psycopg imports and the three frozen calibration/tournament/
  stress artifact hash checks pass inside this new image. Credentials and
  optional build CA are not stored in the image. Smoke container removed.
- JSON/Dockerfile path valid; deploy settings and dependency lock unchanged.

The optional builder value is supported by official config-as-code docs;
removing it does not establish that it was wrong. Cache portability likewise
does not prove the earlier failure's cause without a native build log.
One green PR/main release is attempted. Delivery remains conditional on actual
provider success, matching new main HTTP SHA/assets and independent fresh work
cycles; stale bars do not become current through a deployment. Another provider
failure stops blind release retries. Post-merge evidence is recorded separately.
