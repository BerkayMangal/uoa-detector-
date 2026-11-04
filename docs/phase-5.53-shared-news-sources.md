# Mahalle and Stajyer: verified code audit and proposed shared news contract

2026-11-04. Owner supplies both production URLs as their news applications.
This is a read-only code audit and integration design, not a claim that a new
feed is live or that historical first availability was reconstructed.

| Application / inspected main | Existing access | Correct role |
|---|---|---|
| Mahalle `817017caba7cbaa1ff44371dc172ece63661e4a1` | `/digests.json` cached curated archive; Basic SITE_PASSWORD when configured; `/ozet.json` READ_TOKEN is counts only | Selected corporate/event ideas and explicit model annotations |
| Stajyer `231d16821bc455d051caa79db87e489686eacedf` | owner-authenticated `/api/latest`; public health/source counts; STAJYER_TOKEN also permits costly `/run` | Cross-asset/macro context, not complete ticker-news coverage |

Mahalle collects SEC/company newsroom/IR, regulatory, technology and supply-chain
sources; Grok selects/comments, optional Bigdata corroboration is configured in
code but active entitlement is unverified. It selects at most two stories in
three daily tours. Its digest.at is job start; the 300-turn archive is mutable,
and maintenance can backfill cards/annotations. Preserve source title versus
model title, direct ticker versus derived exposure, publication precision and
backfill stamps. Absence of a selected story is unknown coverage, never no news.

Stajyer collects ticker-labelled Finviz, Motley Fool and 13 RSS sources including
FT, BBC, CNBC, Guardian, Fed, ECB, BLS, EIA, AA and BloombergHT. Notes select only
part of that pool. Current note source conversion drops some ticker/fetch fields;
cache timestamps precede HTTP, 304 refreshes them, and Atom updated may be called
publication. The latest-body cache is not durable revision history. Notes run at
07:00/14:45/21:30 London weekdays; warmup is hourly. These are not a complete
minute-by-minute US company-news service. Publisher content and Grok prose must
remain separate. Never forward the broad owner/run token to the trading app.

## Concrete proposed feeds (NOT existing/live endpoints)

- Mahalle: dedicated read-only `NEWS_FEED_TOKEN` and `GET /news-feed.json`, cached
  archive only. Explicit curated coverage, no-news-evidence=false, stable content
  hash/ETag, publication precision and model/backfill fields.
- Stajyer: separate `STAJYER_FEED_TOKEN` and bounded `GET /api/news`. The first
  delivery exports saved note source-catalog items as `curated_note_sources`,
  not a complete raw feed; missing ticker/time fields remain explicitly unknown.
  It does not export private note prose or invoke collection/model/email jobs.
  A later full raw feed needs append-only packets/receipts before note selection,
  preserving original content identity on 304 and physical post-commit clocks.
- Shared item: provider, source_id, canonical URL, original headline, bounded
  summary, explicit published/updated/unknown time kind and precision, exact
  ticker mapping or unknown, content version/hash, upstream receipt/commit times
  if independently recorded. Model annotations occupy separate optional fields.
- UOA imports each version at its own physical receipt and commit time. Old
  imports become known now, not at their historical publication date. Syndicated
  or copied stories are deduplicated across apps; they are not independent votes.
  Curated/context-only feeds cannot manufacture checked-empty negative news flags.

## Access and remaining work

Both direct production URL checks returned proxy tunnel 403 because these exact
hosts were absent from the cloud environment allowlist. A saved environment draft
adds only `mahalle-production.up.railway.app` and
`stajyer-production.up.railway.app`, preserving the existing five custom domains.
It requires the user to review/save and publish; a draft save does not activate
network access. No hostname/IP/proxy bypass or live HTTP verification was used.
GitHub source access succeeded through the existing authorized identity.

The dedicated context-only feed implementations are being prepared in separate
scratch checkouts and draft PRs. They are not merged, deployed or enabled by this
UOA release. Both use `schema=context_news_v1`, explicit curated coverage,
`supports_negative_evidence=false`, original `source_title`, source URL/identity,
publication precision, separate exact/derived tickers, stable item version hash
and separate model annotations. Unknown first_seen remains null.

No credentials were printed, requested in chat or copied between services.
Dedicated feed endpoints/tokens and actual live schema checks remain a distinct
integration step; the current delivery's physically receipted UW capture works
independently of these two pending feeds. These applications are not labelled as
already connected in the trading policy or the five-arm research dataset.
