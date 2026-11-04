# Phase 5.56 — dedicated shared-news context

Producer releases and UOA code complete; final native consumer evidence is
recorded separately after publication. No live AL threshold, UW five-arm news
experiment, original call/PAPER record, old artifact or protected holdout changed.
No historical incremental news return or validated alpha is claimed.

## Three-service setup

Mahalle #1 is merged at `f4b0de19bb05a473ed25ff6b0526f2c6cbea3dd7`, with
695 tests passed/two existing xfails and exact green feature/main CI. Stajyer #1
is merged at `baa2644bba91b10802c0ffad27573e2e2ecfb4fb`, with 264 tests passed
and a new hermetic CI workflow green on feature/main. Dedicated cached feeds
retain owner/run access separation. Stajyer's citation-only/model fallback title
is now a separately labelled unverified annotation, never a publisher headline.
Mahalle adds authorization-aware cache variation/noindex without weakening old
ETag/auth assertions. No collection, model, email or publisher job was invoked
by a feed request.

Both producer native deployments are SUCCESS on those exact merges. Each feed
passed unauthenticated401, dedicated-auth200, conditional304 and unauthenticated
conditional401. Credentials were independently generated in memory and staged
through scoped Railway variables; none was printed, persisted in a file or
forwarded from an owner/run account. The consumer uses matching dedicated values
and is enabled only after the new code/source checks are green.

## Actual source coverage and limits

Real producer bodies passed the actual bounded consumer transport/parser, not
just a schema mock. Mahalle returned 204 items/465,859 bytes: the fixed consumer
limit retains 100 and explicitly labels limited/truncated coverage. Stajyer
returned 10 items/8,617 bytes, with upstream partial status and no known direct
ticker mappings. Nine Stajyer publisher titles were retained; one unverified
catalog/model title was withheld from the publisher field. All 110 parsed
publisher first-seen clocks were unknown. This preflight made no DB/storage
writes and did not assign physical availability to any item.

The consumer subsequently records its own requested/received/status/body hash,
exact URL/profile binding and post-packet-commit availability, with append-only
item/version associations. Unknown publisher dates/timezones or upstream
first-seen claims remain unverified; an old article received today becomes known
today. A 304 references a prior valid committed200 body and does not advance an
item's first-known time. Native Railway normalizes weak200/strong304 ETags; the
consumer compares the same quoted opaque validator with RFC weak semantics,
while rejecting a different validator. Matching canonical URLs coalesce copies;
different-URL syndication can remain separate and never becomes independent votes.

Collection uses exactly two origins, separate credentials/clients, no redirects,
10-second HTTP/one-MiB/100-item bounds and a 900-second cadence. One writer plus
recording timeout/backpressure prevents a thread backlog. Views use serialized
sessions and narrow metadata queries, loading only chosen bodies rather than
many historical compressed payloads. Failures, corrupt/pending receipts, stale
collection and empty curated responses do not imply no market news or AL.

## Private presentation

`/haberler` shows source connection, physical check time, returned/displayed
coverage, original-source links and separated direct/derived/unknown ticker
claims. Stajyer's upstream partial status remains explicit. Unknown/naive/future
publication and unverified first-seen claims are not presented as facts. The
private `/api/arastirma/haber-kaynaklari` preserves the typed context/provenance.
Both GETs are SQL-only, authenticated/noindex/private no-store. Existing thirteen
module links remain visible alongside the new source page; Phase5.55 presentation
and all open information/expiry guards remain.

## Verification and judgment calls

Final full local gate: **5,823 passed / 52 key-gated skipped**, two existing
warnings, 158.98 seconds. Strict mypy is clean on 324 source files; Ruff and
`uv lock --check` pass. The private source page also passed labelled synthetic
Chromium checks at 1440/390/320 pixels: two source panels, explicit partial state,
all fourteen navigation links, open disclosures and no viewport overflow.

The package's 54 meaningful cases cover exact-host credentials/redirects, bounds,
producer hashes/clock claims, empty/failed semantics, append-only receipts,
versions/duplicate display, repeated200/304 first-known preservation, weak/strong
validators, future-response shadowing and late timed-out writes. Three route
cases cover authenticated read-only delivery, partial/unknown presentation and
escaping/unverified clock claims. Dynamic existing auth tests exercise the new
routes too. Old assertions remain; the existing append-only classification is
extended for the three new forward-evidence tables as required by that test.
New mapped class names are unique so the global model inventory cannot hide an
old Packet/Receipt class through a name collision. No durability rule was relaxed.

Independent source/schema/transport/storage reviews found and resolved those
specific clock/cache/memory/partial-disclosure issues before enabling collection.
Actual native source preflights and safe release metadata are under
`/workspace/uoa-design-review/news56-*`, outside git; no raw producer bodies or
credentials are included. Full local gate and exact PR/main/native/browser
results are recorded at publication.

Next evidence is subsequently observed source/price/PAPER data under separately
fixed comparisons. Source reach, headline presence and duplicate counts are not
a measured trading effect; more publishers alone do not validate a signal.
