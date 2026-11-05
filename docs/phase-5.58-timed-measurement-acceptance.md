# Phase 5.58 — prospective timed measurement and daily accountability

Registered before implementation, 2026-11-05. The owner explicitly authorizes
continuation of the proposed measurement/capture, daily-report and fixed news
comparison work, including ordinary tested publication. Desktop access is not
required for repository/API work. No broker, orders, plan purchase or budget-cap
increase. The 5.52.2 canonical-label repair is already live and remains intact.

## Observed problem and invariant boundaries

At 08:48 London, fifteen first opening AL calls in five sessions have zero
qualified +15-minute marks, one +60-minute mark and fifteen paired day closes.
Nov4 QQQ/AMD/GOOGL nevertheless have 24/24/25 post-call observations. The exact
mix of source age, receipt lag and missed windows is not known. A successful
opening job is not proof of target coverage. Legacy PAPER is a different source
card/instrument simulation; its P&L is not the first-call return experiment.

Create `webapp/timed_measurement/` outside all frozen opening, news and research
implementation directories. Keep their files/profiles/results/old missing marks
unchanged. Do not write OpeningObservation: that shared table can alter later
SPY entry references. Use a separate versioned append-only stream and explicit
read-only UI panels. New coverage is not evidence of improved alpha or of news
contribution. No historical prices, Study G data, retrospective fills, new
hypothesis search or live threshold changes.

## Fixed population, reference and clocks

- A durable activation plus post-commit receipt fixes the capture/opening-profile
  boundary across restarts. Only exact first BUY publications at/after that
  boundary qualify. Verify primary/decision/cycle keys, original Advice, exact
  scan/snapshot/card/profile hashes and first-publication provenance. Updates
  cannot inherit outcomes. Unknown provenance remains unknown.
- Targets derive from original publication + the unchanged opening profile's
  15/60-minute horizons. Keep its 300-second maximum window. A target must lie
  strictly inside the existing regular-session calendar; truncate its effective
  capture deadline at regular close. Holidays, unknown calendars, half-days and
  DST use the existing calendar. Late discovery is recorded; no request occurs
  after the effective deadline or for pre-activation calls.
- Preserve the original planned underlying entry and original scan's SPY price
  known by publication (finite, same session, age<=300s). Missing entry benchmark
  is permanent unknown, never replaced by a new exit quote.
- Preferred fixed source is UW `/api/stock/{ticker}/quote`: last_trade.price
  paired ONLY with last_trade.time (documented Unix ms), regular market phase,
  matching requested/echoed ticker, matching ET date/RTH, positive finite price,
  target<=trade_time<=physical receipt. Reject future, stale, wrong-session,
  malformed or mismatched values; never use option premium/midpoint substitution.
- A maximum of two single-attempt capability probes (AAPL, SPY), no pagination or
  retry, is authorized before collector activation. Capture safe capability and
  quota metadata privately. No fake production BUY/target is created. If quote
  access/schema is unavailable, record a separate engineering source addendum
  before implementing any explicitly labelled completed-1m-close alternative;
  there is no silent within-cohort source fallback.
- Requests/receipt/commit-confirmation times are distinct aware UTC instants.
  Source admission uses physical post-commit confirmation, not an earlier header
  or request stamp. Receipt before deadline with confirmation after it is late.
  Verify request<=receipt<=confirmation; clock anomalies fail closed. Sequential
  stock/SPY observations are near-time references, never synchronous fills.

## Bounded collection and persistence

New `timed_measurement_v1.yaml` freezes operational settings before activation:
enable opt-in; idle poll15s and active poll<=5s; attempt offsets2/32/62 seconds
with >=30s after the previous completed attempt; three paired attempts maximum;
trade age<=60s, stock/SPY exit skew<=60s; request timeout8s; packet body<=65536B;
module reservation cap256/ET day; bounded discovery/reads200; slot-conflict retries8;
PG statement/lock timeouts2000/1000ms. These are capture-quality/resource settings,
not fitted strategy thresholds. Retain shared board daily cap24000 and the native
client rate limit; respect fresh native usage counts. No quota refunds.

Unique append-only activation, target, attempt claim, request slot, packet,
post-packet receipt, pair completion, terminal assessment and day-halt records
provide durable identity. Class names must be unique across the ORM inventory.
Classify every new alfa_ table as append-only in the existing durability test.
Workers initialize tables; GET never initializes or mutates them.

Commit a target/ordinal claim before I/O; replicas and restarts do not replay it.
Next ordinal requires the previous durable completion; a claim without completion
is unknown until deadline, not blindly resent. Each leg claims an atomic daily
module slot plus shared quota reservation before its single HTTP attempt. Native
client internal retries are disabled. Optional native-client request/response
hooks may be added with unchanged defaults, preserving proxy/CA behavior and old
consumers. Reservations are conservative upper bounds, not proven wire counts.

Attempt order is own then SPY; independently record both legs unless a fatal
auth/quota/late condition stops the pair. Packet+receipt durability precedes pair
completion and assessment. Select the first qualifying complete pair in attempt
order, never by return. Never combine own from one attempt with SPY from another.
If no complete pair qualifies, retain only the first eligible own observation as
an explicitly partial outcome once attempts/window terminate. A valid exit pair
with missing original entry SPY remains partial and needs no further quotes.
Assessments are immutable; old missing results and method cohorts are not merged.

Auth/daily-quota/shared-cap refusal halts new requests for that ET day. A temporary
429 is recorded and respects a60s cooldown plus the fixed remaining ordinals;
there is no immediate/internal retry. Transport/invalid-source attempts remain
explicit. Cancellation, crash, recording failure and provider refusal never
manufacture a value or mark a missing outcome as a zero/loss.

The independent failure-isolated worker uses existing native service isolation;
blocked DB/HTTP work cannot stall ASGI liveness or spawn overlapping unowned I/O.
Bindings and claims are durable; local locks alone do not establish ownership.
Public health exposes safe aggregates only. The enable flag is configured only
after code validation; its durable activation timestamp is proven natively.

## Read-only report and honest user experience

Add a compact private `/olcum` daily report and `/api/olcum`, plus an additive
fifteenth visible navigation link. Preserve all existing modules, open details,
forms, alerts/consent, first-entry and expiry guards. Per-call cards link/show
new capture status with the method/activation clearly separated from old marks.
Selected dates and reference times are explicit Europe/London; existing TR
session displays retain their labels.

Report first calls, eligible/not-applicable targets, due/pending/mature, own
observed and SPY-paired counts, timing/source/receipt lag and rejected/missing
categories (including missing entry SPY versus missing exit SPY). Zero targets
has no success rate/return; missing is never zero. Show sampling/limited flags.
Keep new targeted references, legacy gross day-close marks and model-cost PAPER
closures as separate scopes/groups. PAPER is not a capital-constrained real
account, and its policy/instrument cohorts must remain separate.

News panels show actual stored source connection/receipt status and unchanged
UW fixed five-arm readiness. Correct obsolete shared-source connection copy.
Mahalle/Stajyer remain context only; the new capture stream does not upgrade or
feed the frozen news experiment without a separately fixed source protocol.
No conditional win probability, learned model, new winner or return improvement
is claimed from today's small/partial sample.

## Verification and publication

Meaningful tests cover exact provenance, first/update/activation boundaries,
restart and concurrent claims, single physical-attempt budgeting, caps/refusals,
raw packet/receipt ordering, late commit, both clocks/skew/RTH/DST/half-days,
missing baselines, per-leg errors, partial/full pair selection, future/invalid
data, cancellation/crash/blocked-I/O isolation, unchanged frozen hashes, GET
SELECT-only/no vendor calls/auth boundaries and honest empty/limited reports.
Preserve old assertions. Full hermetic pytest/mypy/Ruff/lock gate before every
commit; exact green PR/main CI; normal manual merge, no admin/auto/force.

Native proof requires exact deployment SHA/SUCCESS, healthy completed scan,
the real durable activation/worker status, private/public boundaries and actual
desktop/390/320 rendering with fifteen visible module links. Probes/fixtures are
not live success. Before the first real qualifying post-activation target, state
that outcome capture remains awaiting its first live observation; never invent
a signal, filled result or retrospective activation to demonstrate it.
