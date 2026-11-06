# Phase5.64/5.64.1 — prospective candidate price capture

Implementation verified; exact feature/main CI and native release evidence is
appended to the PR after publication. This delivers a prospective measurement
path, not a calibrated winner or a claimed improvement in returns.

## Resulting behavior

The frozen November4/5 pilot retains286 scans/3651 candidate rows,8 slots and0
fully priced units. Its daily report and the published-call measurement method
are unchanged. New `shadow_price_v1` observes fresh completed post-activation
LIVE scans after open+15, selecting known-base candidates before RS/news filters.
First successful ticker admission per ET day, at most4 daily, is immutable.
Original source/profile/hash and physical news provenance remain verifiable.
This observed known-base cohort is a different estimand from the frozen roster.

An isolated background worker schedules entry at confirmed admission+60s and
15/60-minute references after the verified entry pair's confirmation. Each
attempt reserves shared and dedicated quota before wire I/O and captures own
ticker/SPY from the existing bounded native quote transport. Raw bytes, hashes,
request/receipt clocks and dual post-commit receipts bind each reference. Source
last trades must be regular-session, at/after target, no more than60s old and
within60s of one another. Target windows clip to the actual session close.

Unique append-only claims prevent replay across concurrent workers/restarts.
Unconfirmed/future ownership stays reserved; lost requests, auth/daily/unknown
quota or unknown wire outcomes halt conservatively. Known rate limits cool down.
The96/day module cap does not increase the existing shared daily cap. Maximum
three timed attempts per phase, no leg mixing, fallback or old-price repair.
Only activation can finish missing confirmation on restart at actual new time.

Existing private `/arastirma` includes a compact, separate candidate block;
`/api/arastirma/aday-olcum` exposes the same read-only projection. It distinguishes
pending, missing, outside-session and unverified references, with own/SPY returns
and their difference. Optional2500USD whole-share,5bp/side arithmetic is a stock
reference only: no option return, executed fill or portfolio result. No new menu,
alert controller, scoring rule, profile threshold, automatic training or broker.

## Verification

Final unchanged gate: **6416 passed,52 key-gated skips,two existing warnings**,
201.04s. Strict mypy349 files,Ruff,lock and whitespace checks pass. This adds
144 focused source/storage/collector/view/integration cases and15 automatic
private-route authentication cases. Synthetic active-empty and fully observed
fixtures render at1440/390/320 with15 navigation links,one alert controller and
no horizontal page overflow, including expanded disclosures. GET causes0 writes;
the observed synthetic chain makes exactly6 fake quote calls. Fixture returns
are not market evidence.
Focused coverage includes actual source scan/news→admission→entry/15/60 raw
pairs→private SELECT-only API, independent return arithmetic, duplicate/future
ownership, delayed commits, malformed/tampered sources and receipt integrity,
session boundaries, quotas, rate limits, unknown wire outcomes and restart.
Both tables are registered in the existing append-only durability assertion.
Ordinary lifespan tests disable the new worker; explicit worker tests opt in.

## Remaining empirical limit

At publication this path may be armed with no real post-activation candidate.
Do not fabricate a capture for release evidence. Five-minute source cadence and
upstream availability still limit which candidates can be observed. Captures
are paired last-trade references, not exact minute opens, exact-time benchmark,
option NBBO or executable prices. Stored RS/news flags enable later fixed
comparisons; their marginal benefit has not been established. Preserve prior
negative results, frozen80-session gates and reserved StudyG/raw labels.
