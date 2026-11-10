# Phase5.67 — operational measurement coverage

Implemented independent bounded settled-data maintenance, original-packet news
metadata diagnostics and historical-session read links. Exact final CI, native
SHA/deployment, current coverage and original-Nov9 record-parity evidence are
recorded in the release PR after publication. No strategy is promoted or retuned.

## Observed status before changes

The first Nov9 session has two candidates, GOOGL/MSFT, eight timely verified
four-arm decisions and all six entry/15/60 reference phases observed. Base net
hypothetical stock-reference totals are−6.73311USD at15minutes and−14.48923USD
at60minutes. These are alternative holding horizons, not cumulative daily PnL.
The three filtered arms are UNKNOWN for both candidates; there is one stored
daily result but zero complete common sessions. No calibrated edge follows.

GOOGL's actual model output contains a confirmed positive product event and a
mixed analyst item with unknown certainty. The registered guard correctly
abstains. MSFT's original known-false source did not satisfy the stronger
checked-empty guard; its exact metadata cause was not established from the old
projection. This release reveals the original bound packet's metadata, never
changes either stored decision or treats missing evidence as a cash-zero win.

The Nov10 preopen daily archive again lacked the expectedNov9 closes despite
Nov9's explicit missing-data repair. The old daily job's success counts do not
prove expected-session completeness, and the same-day schedule does not
explicitly catch up yesterday on the next morning. The exact preceding failure
could be missed execution, stale payload or side-capture failure; not proven.

## Resulting behavior

The opt-in `daily_guardian_v1` service reads required-session coverage and exact
physical DailyReceipts in a fixed05:00–09:15NewYork window on verified trading
days. Full coverage means zero vendor calls. Missing current data can invoke the
existing collector once under a deterministic settled-session request ID, with
unchanged82requests/5000quotareserve/0.75sspacing/30stimeout/60-session bounds.
Any missing or altered required receipt, invalid existing bar, unknown calendar,
limits drift, pending owner or previous same-session attempt prevents new work.

A shared PostgreSQL session advisory lock serializes guardian and explicit
manual startup operations across processes. Unlock uncertainty invalidates the
physical connection. Both use the existing durable claim before any call;
partial/failure/orphan outcomes never automatically replay. A new manual request
ID does not itself clear an unfinished prior claim. The original `once()`
interface remains backward compatible with an optional injected runner; the
unchanged default collector is used for original direct/manual calls.

Guardian's new runner uses the original collector and transport with an httpx
pre-send hook. It checks window/clock/settled-session/bounds after spacing and
before every actual request, stopping with a durable result at the boundary.
All inserts/receipts keep actual new response/commit times; conflicting vendor
revisions remain separate and never overwrite old rows or backfill decisions.
No source/strategy/capture identities or existing numerical rules are changed.

`/api/arastirma/veri-gozcusu` is an authenticated, SELECT-only safe status view.
`/api/motor/haber/tani?day=...` reads at most four original source-bound packets,
verifying old identities, original verifiers, receipt signatures, packet hashes
and bounded8MiBgzip metadata. It reports counts/limit/pagination booleans and
source-check clocks/reasons, with no headline/body/URL values or model/provider
call. It lives outside frozen component hashes and never supplies a policy vote.

`/arastirma?day=...` now shows that session for the forward source, decisions,
news, fundamentals and diagnostic views. Previous-session/current-session links
preserve15main links and one notification controller, with no new script/form/
nav element. Future requests fail before a database read; navigation does not
extend before this cohort's activation. Existing historical-study/pilot totals
keep their original populations. Plain-language source explanations stay folded.

## Verification and remaining limits

Tests cover durable ownership, verified coverage, missing/altered receipts,
manual concurrency, PostgreSQL lock refusal/unknown-release cleanup, actual
post-spacing per-wire cutoff/rollback, default-runner compatibility, private
SELECT-only routes, historical date propagation and no future database read.
Diagnostic cases cover full stale pages, pagination URLs without leakage,
freshness-clock boundary, original verifier precedence, signatures/hash/ticker
mismatch, malformed/oversized/deepJSON and fixed safe exception codes.

The old source capture identity and frozen vendor/fusion research report seals
remain exact. Native proof must also compare all four saved Nov9 row sets and
prices; new guardian metadata is not statistical progress or a fitted signal.
A first week remains a descriptive operational review, with the original80new
complete-session gate, no winner and no automatic promotion. Option/broker PnL
cannot be inferred from these stock reference units.

Full unchanged local gate: **6957passed,52key-gatedskipped,2existingwarnings**,
strictmypy391files, Ruff/lock/whitespace clean. This adds109 passing cases
over6848. Independent Decimal reconstruction of the four Nov9 control-arm
15/60 net amounts matches all stored entry/exit prices, quantities and costs.
