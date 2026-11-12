# Phase 5.70 — Automatic signal measurement and repeatable research

Frozen before implementation, 12 November 2026. Owner explicitly asks to
simplify the broken workflow: automatically collect, show signals, measure
them and keep testing with backtests. This authorizes the implementation;
merge/deployment follows the repository's explicit owner approval boundary.

## Product contract

1. Existing US signal generation remains the source of decisions. An additive,
   supervised background process queues immutable recorded opening-entry BUY
   decisions for Theta validation without owner clicks. It binds to the actual
   saved plan contract and physical decision availability, with a fixed entry
   delay; it does not substitute a dominant flow contract or choose a winner
   after seeing prices. Multiple-leg, unavailable, invalid and unsupported
   plans are explicitly excluded and counted. Existing strategy profiles and
   recommendation thresholds do not change.
2. Admission is bounded and idempotent, survives restarts, and retains source
   identity and the reasons for exclusion. Historical catch-up is identified as
   retrospective. No new signal is invented when no eligible saved entry exists.
   Jobs are not re-created on every poll or silently removed because they lose.
3. Keep the Phase 5.68 quote economics and fixed +15/+60 minute horizons.
   Prices come from actual validated provider payloads; missing prices stay
   UNKNOWN. Report one-contract quote markouts, not broker fills or portfolio
   profit. The complete admitted cohort (including pending/excluded/unknown)
   is available in a bounded stored-data export with stable identities, hashes,
   timestamps and method, for reproducible reruns. Results must show coverage
   and may not infer edge from the priced subset alone. A stored-data rerun
   does not launch an unbounded vendor download or optimize a strategy.
4. A separate explicit continuous-device enrollment works before any job is
   mature. Its one-use grant lasts 24 hours. The revocable device credential
   authorizes future Theta validation jobs, uploads and heartbeat only, until
   revoked. Only token hashes are stored server-side. Existing v1 pairing,
   fixed request scope, seven-day expiry and tests remain unchanged. Owner
   administration, reports and request creation stay behind owner auth.
5. A device is not 'connected' merely because a file was downloaded. Record
   bounded heartbeat state separately from quote receipts and distinguish
   bridge online, local Terminal reachable, actual validated data received,
   idle, stale/offline and revoked. Device-reported health is not proof of
   provider entitlement or successful pricing. Never log credentials or raw
   provider error bodies; public setup pages are no-store/noindex.
6. The new standalone connector supports one-time user installation, automatic
   user-session restart, bounded reconnect/backoff and identical-body upload
   replay. User-local files containing credentials are private. UOA origin is
   pinned, local Terminal stays on loopback, HTTPS/redirect/body/time limits
   remain enforced. A Terminal transport outage is reported as health before
   consuming job attempts; it must not permanently exhaust future jobs just
   because the Mac was offline. No public Terminal tunnel or credential scraping.
7. The main Theta screen presents automatic collection, measurement coverage,
   results and stored-data research controls. Manual historical job forms are
   secondary. Read-only status refreshes automatically. Keep the existing
   homepage and measurement page reachable and separate stock/option results.
   One actual Mac setup action remains necessary; do not claim remote Mac
   access, zero-dependency installation or live Theta success without evidence.

## Verification and limits

Verify causal source identity and timestamps, single/multiple-leg handling,
restart/idempotency and bounded catch-up, full-cohort missingness, stable reruns,
empty-queue enrollment, legacy scope isolation, revocation/auth route isolation,
heartbeat freshness, transient failures/replay and desktop/mobile behavior.
Run the complete hermetic pytest, strict mypy, ruff and lock gate before commit.
Real Terminal ingestion is a separate live verification and cannot be simulated
into production. No broker orders, automatic trading, profitability promise,
threshold tuning or deployment of the separate Phase 5.69 research archive.
