# Phase 5.32 — desk data reliability

Frozen before code, 2026-10-27. Owner approves the preceding five improvements
and calibration: "tamam ozaman sirayla yapiyorsun ...". Deliver and deploy each
ordered package without another approval. No broker execution is authorized.

## Scope

1. Honor the existing news refresh interval for FAILED reads too. The current
   implementation explicitly bypasses its cache after a failure and retries
   every cycle, contrary to the existing maximum refresh cadence. Preserve the
   failed state and original checked_at, never convert failure into no news.
2. Preserve typed, sanitized HTTP/error categories without storing credentials,
   provider response bodies or arbitrary exception messages. Reuse the existing
   append-only news-check records; no alteration of historic rows or schema.
   Summaries distinguish access denial, invalid request, quota/rate, transport
   and response-shape failures. Do not guess the production cause before seeing
   evidence. The existing live-profile thresholds and decision paths are frozen.
3. Aggregate per-cycle news error categories and lab exclusion reasons. The
   lab's recorded rules remain unchanged; do not loosen them to produce a sample.
   A candidate may have multiple exclusion reasons; counts are not disjoint.
4. Private home/lab pages give concise Turkish source/rejection explanations.
   Public health may expose only predefined category counts and job timestamps,
   never tickers, headlines, prices, plans, source bodies or exception messages.
   No additional UW requests or public SEO exceptions.
5. Deploy this package and read the actual safe production diagnostics. If the
   vendor denies an entitlement/key, report that specific external prerequisite;
   do not substitute fabricated news or bypass access checks. Continue the
   independent position/research work authorized by the owner.

## Required verification

Meaningful tests: failed-read cooldown and retry after expiry; successful cache
preservation; typed error categorization including 401/403/404/422/429 and malformed
payload; private rendering and no raw data in public summaries; exclusion counts
match frozen audit inputs; unchanged live and PAPER behavior. Run repo gate,
green PR/main CI, merge, Railway SHA and actual diagnostic verification.

The next packages implement the approved position/risk/benchmark/scenario and
calibration work under separate frozen contracts. No number of historical
trials changes the existing rejected-study verdicts or proves prospective alpha.
