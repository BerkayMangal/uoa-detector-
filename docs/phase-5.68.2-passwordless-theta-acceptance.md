# Phase 5.68.2 — Passwordless Mac connection

Owner request, 12 November 2026: cannot find or enter UOA credentials and asks
to remove that step. Prior authorization to integrate and deploy persists for
this usability repair. No trading rules or quote economics change.

1. An authenticated owner can issue a two-hour, single-use pairing link for
   at most 32 already pending Theta requests. Tokens have 256 bits of randomness;
   only their hashes are stored. No general site credential is downloaded.
2. A public, no-store, noindex landing page receives the pairing token only in
   its URL fragment, removes it from browser history, and requires an explicit
   click to redeem. No external resources, analytics or browser storage.
3. Redemption atomically admits one connector session and downloads the
   standalone Python program with its restricted opaque credential. The
   credential expires after seven days and grants only pending-job reads and
   result uploads for the frozen request IDs. New requests are not inherited.
4. All other data, issuance, retry and administration routes retain the existing
   owner authentication. Jobs are filtered before applying the batch limit.
   Missing storage, invalid credentials and expired grants fail closed.
5. The personalized program asks for no username/password, pins its UOA origin,
   never sends its credential to Theta Terminal, and retains request bounds,
   TLS verification, redirect rejection and sanitized errors.
6. Pairing/session records are additive and append-only. Duplicate redemption
   is rejected even across concurrent requests. The current session boundary
   is seven-day expiry; site password rotation is not claimed to revoke opaque
   sessions. No general-purpose account access is granted.

Required verification: expiry and single-use behavior, route/ID isolation,
no secret persistence, connector host pinning, browser fragment handling,
end-to-end credential-free download and bounded connector job access. Full
repository gate before commit, PR CI before the already authorized deployment.
Real Theta prices still require the Mac Terminal to be running.
