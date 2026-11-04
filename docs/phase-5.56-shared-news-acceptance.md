# Phase 5.56 — physically observed Mahalle and Stajyer context feeds

Frozen before UOA implementation/source activation, 2026-11-04. The owner now
explicitly requests completing both connections and authorizes managing other
sources if needed. This supersedes the earlier deferral recorded by Phases5.53–
5.55. Do not alter those frozen records. Initial delivery uses the existing two
owner-operated feeds; no new paid provider, purchase or external model request.

## Producer release

Finish the existing dedicated cached context_news_v1 feeds through their ordinary
PR workflows: Mahalle #1 current head549511402d169810b083624bdb73dcc534de7d06;
Stajyer #1 head58c4f78c865f67b5272f1fe5505c210d2a80dc15. Verify exact-head tests
and existing CI, mark drafts ready and merge manually under the owner's explicit
authorization. Do not force/bypass protections. Verify actual native repository,
production domain, deployment SHA/status and authenticated feed schema.

Use independent dedicated read-only feed credentials: Mahalle NEWS_FEED_TOKEN
and Stajyer STAJYER_FEED_TOKEN. Never forward SITE_PASSWORD, READ_TOKEN, owner/run
tokens, cookies or broad credentials to UOA. Reuse an appropriate configured
dedicated token if valid; otherwise generate cryptographic random secrets in
memory and configure matching producer/consumer variables through the existing
Railway API. No secret values enter chat/logs/files/git or public routes.
Feed GET/HEAD must remain cache-only: no collection, model/email/job invocation,
DB mutation or /run call. Empty/missing/colliding credentials fail closed.

## Consumer and physical evidence

Create a separate typed shared-news adapter/storage and fixed profile. It polls
only the two exact configured HTTPS endpoints at a bounded background cadence,
with timeouts/body/item limits and no redirects. No arbitrary URL or credential
forwarding; credentials go only to their corresponding allowlisted origin. UOA
GET routes never fetch/optimize/write. Keep the collector independent of live
signal latency and fail closed on source/recording errors; no thread backlog or
unbounded retries. Honor runtime proxy/CA policy, existing DB memory isolation and
one-worker collection limits. Record requested/received/status/body/version hashes
and post-commit physical availability in append-only private tables. A response
received now does not acquire its old publication or producer job-start time as
historical availability. A failed/pending write is never a successful receipt.

Validate context_news_v1, provider identity and explicit curated coverage with
supports_negative_evidence=false. Preserve original publisher title/source/id/URL,
raw publication claim and precision, unknown first_seen, exact versus derived
tickers and optional model annotations separately. Do not promote prose, inferred
exposures, naive/date-only/updated timestamps or cached note/job times into
verified original ticker-news publication. Invalid/future clock claims remain
unknown or reject; no backdating. Deduplicate syndicated/cross-app articles for
display without erasing provider/version provenance; two copies are not votes.

A 304 is an unchanged-content transport observation only, usable only with a
previous valid successful local body; it cannot manufacture new content/first
availability or bypass authentication. Errors/empty curated feeds never mean
there was no relevant news in the market. Persist limits/truncation/failure
categories, and keep physical collection freshness separate from article age.

## Role and presentation

These are source-labelled corporate/macro context, not automatic AL votes.
Existing live news/RS/price/flow thresholds, current OR rule, five-arm UW news
experiment, original calls/PAPER records and old artifacts remain unchanged.
Do not mix these curated receipts with the UW exact-ticker packet counts or
claim a measured incremental effect before subsequent separately fixed evidence.
Private UI shows producer connection/last successful physical receipt, coverage,
latest original-source headlines and links with exact/derived/unknown mapping
clearly distinguished. No public data/credential exposure. Preserve full visible
navigation/open information and the Phase5.55 clearer presentation. Public SEO
remains static. Missing data stay unknown, never zero performance or safe-to-buy.

## Verification

Meaningful tests: schema/source identity, secrets/redirect/host isolation, body
bounds, malformed/naive/future timestamps, failed/unknown versus empty semantics,
append-only packet/receipt timing, duplicate/version/304 behavior, storage timeout
failure isolation, authenticated read-only views and no change to live policy.
Verify both producer suites and exact available CI plus the complete UOA hermetic
pytest/mypy/Ruff/lock gate. Publish ordinary feature PRs with green exact PR/main
CI and native deployment SHA, then authenticated GETs and actual persisted packet/
receipt counts. Preserve all previous assertions. No Study G access, reserved
historical price extension, additional retrospective search or profit claim.
