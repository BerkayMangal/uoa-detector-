# Phase5.64 — prospective pre-filter shadow price capture

Registered before code,2026-11-06. Owner explicitly directs fixing missing
timed prices, not another readiness-only report. Existing frozen pilot has8
unpriced slots; existing timed capture observes published BUY calls only.
Preserve both methods, old missing marks, daily reports, all live rules/profiles
and reserved StudyG/raw labels. No broker, synthetic production call or purchase.

## New independent observation method

Method shadow_price_v1 starts at a durable, physically confirmed activation.
Observe the most recent completed LIVE scan at most60 seconds old, started after
activation and after open+15min, with verified source/profile/machine context.
Use the unchanged news adapter's flow+price predicate. Admit only known true
base opportunities; preserve original RS/news true/false/unknown flags before
either auxiliary filter. Exclude SPY itself (benchmark only). Chronological
observation order within a scan is ticker order; the first successfully admitted
candidate per ticker/ET day is immutable. Maximum4 candidates/day across workers
and redeploys. Record source hash/scan, original feature clock, discovery clock
and post-commit availability; stale or missed scans cannot be replayed later.
Known-base sampling is a new estimand, not the old first-non-false frozen roster.
It does not assert complete market/universe discovery or causal news benefit.

## Dedicated timed references

Entry reference is scheduled60s after candidate confirmation; exits15/60min
after the entry pair's post-commit confirmation. Capture the own ticker and SPY
via existing verified native /api/stock/{ticker}/quote. Reuse bounded single-wire
QuoteTransport and parse_quote unchanged, retaining raw bytes/hash and physical
request/receipt/post-commit clocks. Require regular-session source, matching
ticker/date, positive finite price, Unix-ms last_trade timestamp at/after target,
not future and at most60s old. Pair source skew<=60s. These are paired last-trade
references, not minute opens, exact-time SPY, option NBBO or broker fills.

Clip each target's300s window to the verified RTH/holiday/half-day close. Explicit
not-applicable/expired/unknown statuses replace neither prices nor losses.
Attempts at+2/+32/+62s, max3, at least30s between completed attempts; never replay
a claimed attempt after timeout/crash. Persist ownership before wire I/O and
confirm packets after commit. Never combine legs from separate attempts. Keep
entry and each horizon distinct; no old-price interpolation/substitution.

Reserve each possible request before send against a dedicated96/day module cap
and existing shared daily soft cap. Cross-worker unique request/slot identities
bound duplicates; unspent reservations are not refunded. Auth/daily/unknown quota
refusals halt the day; known rate limits impose60s cooldown. Lost/incomplete old
ownership blocks new requests, then halts on deadline. Native timeout/unknown
wire outcome halts conservatively. No fallback provider, implicit HTTP retry,
credential change or quota increase. Timeout8s/body65536B per native request.

## Storage, analysis and lifetime

Use a separate append-only event/receipt namespace with typed record kinds,
checksums, bounded reads and unique day/slot/ticker/attempt/request claims.
Activation may be confirmed on restart at the new actual time if originally
unconfirmed; old candidate/packet/outcome receipts cannot be manufactured later.
Keep raw vendor bodies private. No writes to old opening/timed/news tables.

An isolated background observer/collector runs independently of live advice;
GET performs only bounded verified reads, no DDL, HTTP, targets or assessment.
Public health contains only safe counts/status. Add a compact separate block to
existing research page and private API /api/arastirma/aday-olcum. Show durable
activation, source-qualified shadows, entry/15/60 coverage and paired own/SPY
references. Separate groups by method/profile/activation and horizon. If shown,
USD2500 whole-share5bp/side reference PnL and fixed five-arm membership remain
descriptive; unknown eligibility/marks stay unknown. No portfolio/option PnL,
winner, conditional success probability, automatic training or rule promotion.

## Verification and publication

Test real source scans/physical news receipts, known/unknown/stale/profile and
activation guards, four-slot concurrency/deduplication, raw/receipt integrity,
source clocks/RTH/DST/close boundaries, own/SPY pairing and independent return
arithmetic. Cover shared/module quota, refusal/cooldown/timeout/crash/restart,
current-claim bootstrap ownership, late commits, GET SELECT-only/auth and actual
empty/waiting UI. Register new tables for durability; preserve old assertions.
Full unchanged gate before commit, exact green feature/main CI, normal manual
merge and native fullSHA/SUCCESS/healthy source+collector, actual durable
activation and desktop/mobile/API proof before delivery complete. Until the
first real eligible post-activation candidate and capture, explicitly say armed
and awaiting real observations; never manufacture one for publication proof.
