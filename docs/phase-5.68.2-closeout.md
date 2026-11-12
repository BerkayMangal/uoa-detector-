# Phase 5.68.2 — Passwordless ThetaData handoff

**Code complete; real ThetaData ingestion remains open.** The owner explicitly
asked to remove username/password entry after authorizing the integration and
deployment. This follow-up delivers a bounded no-login download instead of
placing general UOA credentials in the Mac program.

The owner issues a two-hour one-use pairing link. The public page keeps its
token out of HTTP URLs, removes the fragment, and redeems only after a click.
The downloaded connector receives a seven-day opaque credential restricted to
the frozen roster of up to 32 pending mature research requests. Only token
hashes are stored. It cannot read other UOA reports, create requests or place
orders. New jobs require a new grant. Existing owner authentication is retained.

Judgments:

1. Use opaque random credentials and database uniqueness, rather than signing
   with a potentially human-chosen site password. Redemption is atomic across
   concurrent requests; records are additive and append-only.
2. The connector session expires after seven days. Site password rotation does
   not implicitly revoke it; no immediate revocation UI is claimed.
3. Public landing/JavaScript and grant redemption are exact method/path
   exceptions; all other owner routes keep the existing gate. The independent
   route-enumeration test records this intentional new contract.
4. A configured canonical UOA origin is embedded, never an incoming Host header.
   The program rejects a different origin override and sends no credentials to
   the local Theta Terminal.
5. Each personalized download uses a random unique filename and displays the
   matching command. This avoids running the user's older password-prompting
   download after the browser creates a duplicate filename.

Validation: **7,257 passed, 52 skipped** in the full hermetic gate (386.69s),
strict mypy clean on 399 source files, Ruff and lockfile checks clean. Skipped
provider tests are not live-access verification. Relative to Phase 5.68.1,
the suite has 60 additional passing cases net.

Independent review and focused tests covered expiry, replay, concurrent
redemption, immutable scope, filtering before batch limits, no credential
prompts, origin pinning, denied owner-route access, and sanitized failure.
Actual local HTTP + Chromium tests at 1440px and 390px confirmed no site login,
no automatic redemption, fragment removal, unique file download, matching
command, protected owner routes and replay rejection; no JavaScript errors or
horizontal overflow. No synthetic receipt is submitted to production.

The remaining external step is to run the personalized file beside the owner's
authenticated Mac Theta Terminal and verify its real provider packets. This
repair does not claim an investment edge or change any signal threshold.
