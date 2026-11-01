# Native recovery verification — 2026-11-01 UTC

The actual production failure chain was isolated with Railway GraphQL metadata,
build/runtime logs and authenticated application requests. No credentials are
included. The previously observed Cloudflare 1010 request failure was resolved
by a truthful application User-Agent. This supersedes the management-access
limitation recorded at the start of Phase 5.51; no access denial was bypassed.

1. Unsupported Docker secret mount caused build rejection. PR #121 removes the
   optional `type=secret,id=build_ca` mount; pinned images and TLS verification
   remain. Native build then succeeded.
2. Railway's exec override passed literal `$PORT` to uvicorn. PR #122 explicitly
   invokes `sh -c` and `exec`, with the same default port as Docker CMD. Both
   configured/default ports and process replacement have regression coverage.
3. The app's configured database is **not** either neighboring Postgres service
   in unique-balance. Its actual rendered TCP endpoint resolves to the existing
   **merry-consideration / Postgres** deployment, verified against workspace
   service-proxy metadata. It had been removed after a clean SIGTERM. The same
   prior image and attached existing volume were redeployed; no database URL,
   password, volume, records or shared service configuration was changed.

Observed native recovery:

- App main `7dcd3059202114b23812acc6ad59a40b0b5b110c`;
  deployment `b3daa430-b6ae-492a-99eb-d25d33dfd0a2`, SUCCESS.
- Existing database deployment `dae7a995-94db-4aa9-b57c-faaa64c61d99`, SUCCESS.
- Main CI 41361524035, SUCCESS; PR #121 and #122 CI also green.
- GET /health agrees with deployed SHA. Authenticated /, /acilis, /karne,
  /arastirma and /sonuclar all returned 200 with private/no-store and noindex.
- Live Alpha's existing 30-second supervisor recovered automatically after the
  correct database returned: status ok, CLOSED, 34 cards and healthy downstream
  desk/calibration/tournament/opening states. No redundant restart-loop code.

The earlier database shutdowns were clean external lifecycle events. Their
initiator is not established; this record does **not** promise they cannot recur.
No evidence justified a storage reset, empty replacement DB, billing increase or
reconnecting GitHub. Adjacent databases were left alone. Subsequent feature
release verification must use its own exact SHA, not this earlier health receipt.
