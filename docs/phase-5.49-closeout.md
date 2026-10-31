# Phase 5.49 closeout — call evidence and measured hypotheses

Code/research complete, delivery requires actual matching production evidence.
Existing owner authorization covers development, push, merge and release.

## Shipped behavior

- Private /karne defaults to canonical first AL records; ticker search and all
  updates remain available. /karne/{key} reads original advice/source timestamps,
  verifies source snapshot/card/profile/availability, shows recorded per-unit
  stock/call/spread costs and preserves first-entry outcome ownership. Later
  updates link to the first record without borrowing its returns. Observed same-
  session favorable/adverse underlying moves use only later immutable price
  observations, with sample count and unknown values when no subsequent price
  exists; they are not full-tick extrema, option P&L or fills.
- Private /arastirma reads the frozen 144 replay summary, all cost/horizon
  sensitivities, descriptive uncertainty, independent forward records and daily
  per-name coverage. It never runs an optimizer or writes records on HTTP.
- Existing supplemental-close provider entrypoint and startup kwargs remain
  compatible; it adds the registered 70-name universe, NFLX and 11 sector ETFs
  (82 research names, deduplicated with existing basket/board/hot-list names) to
  the existing scheduled daily job/error/cap policy. No extra polling loop or
  paid tier. Production coverage is not claimed filled without observations.
- Separate four-arm shadow cohorts start with signals on/after 2026-11-02.
  Full core coverage, exact sessions, first-writer rosters and a post-commit
  receipt prevent partial-arrival reranking/backdated publication. Reference
  prices require exact mature entry/exit dates and matched SPY/QQQ observations.
  Units stay separate from the archived portfolio and live PAPER/BUY policy.
- Legacy daily capture timestamps can predate actual receipt; new immutable
  physical first-seen receipts plus payload hashes conservatively prove when
  those prices were available. Old calls never acquire later data. New as-of
  prior-session SPY/QQQ/sector context is separately recorded, not painted onto
  historical calls. Single-flight/bounded background failure stays isolated.

## Actual research finding

See phase-5.49-results.md: all 144 fixed replays calculated from 17,640 archived
rows / 70 names / 252 dates, 64 stock candidates. Retest loses in all primary
windows. Persistence audit +$882.36 becomes -$63.86 without its best win and
-$234.35 at 50bp/side; development is negative. No robust alpha, no promotion.
Report hash b32a840ebdf92e31140385300e367a68b558bb4fc897372a2e73572c2c9e50da.
Source/profile/implementation hashes, compressed ledgers and diagnostic counts
are persisted. No reserved Study G price read, threshold retune, historical NFLX
or option-price fabrication. Existing scientific artifacts/code/profiles remain
unchanged.

## Validation and judgments

Full gate: 5392 passed, 52 credential-gated integration skips; strict mypy 288
files, ruff/lock/diff clean. Twenty-eight new explicit cases plus existing
schema-driven parameterizations cover prefix causality, retest close-before-
entry, exact calendars/missing exits/split boundaries, whole shares/cash/slots/
matched references, all 144 independently reconciled ledgers, reproducible
bootstrap, as-of source proof, context provenance, concurrent first writers,
late write receipts, mature idempotent pricing, cost arithmetic, canonical
outcomes, no false zero movement, auth/noindex/no-store and no HTTP writes.
Existing test assertions stay intact; the schema-policy ledger gains four
append-only classifications and its explicit model import.

Actual Chromium 1440/390px: six list/scorecard/research renders, collapsed and
expanded 144-row sensitivity region, keyboard secondary navigation, no page
horizontal overflow, zero WCAG A/AA axe violations and zero JS errors. Backtest
values are the actual frozen artifact; call records are visibly labelled local
fixtures. Local server stopped and temporary DBs removed. No production API,
DB or login credentials were used or requested in chat.

Judgments: pre-register four fixed comparisons, not a search-until-positive
loop; use daily close confirmation followed by a later open rather than a fake
intraday retest fill; retain failed arms for prospective comparison; require
physical price-availability and post-commit publication evidence; preserve the
startup provider API instead of weakening its old lifecycle test.

Release requires green PR/main CI and actual matching Railway SHA/assets. If
blocked again, report it rather than invent success or make blind retry commits.
Next evidence is genuine prospective records and complete capture coverage, not
another optimization on the already-used audit period.
