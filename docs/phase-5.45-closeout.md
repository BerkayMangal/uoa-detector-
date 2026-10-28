# Phase 5.45 closeout — opening decision desk

Implementation complete; production observation coverage remains blocked by the
independent PostgreSQL incident until actual durable writes recover. No claim of
calibrated alpha, execution, new live observations or profitable strategy.

## Delivered behaviour

Private `/acilis` and home brief use the declared NYSE calendar: preparation at
open minus 20min, new entry assessment after the first 15min, exits without that
delay. On October 28 these are 16:10 / 16:30 / 16:45 Istanbul; ET/DST, holidays and
early closes are read from the existing calendar. This is a timing overlay, not
an opening-range/VWAP model: minute bars have not been invented.

A committed live_alpha_v1 card must pass current-session price, per-name flow,
successful news-check, source identity, instrument/whole-size/risk and original
option quote/cost gates. Stock plans are bullish purchases; bearish new entries
are ready PUT/debit-spread purchases, never sales of an unowned holding. Manual
SELL/CLOSE references an existing journal position only. PAPER exits stay
explicitly virtual. Fresh exit conflicts block entry. Newer cached adverse
prices, expiry, a changed scan or a new session withdraw displayed instructions.

The worker adds structured per-name flow/news metadata to existing immutable
source scans, with no extra vendor calls or quota increase. An independent,
bounded/single-flight observer checks the actual committed source ID even after
a later research step fails. Concurrent superseded scans are journaled as WAIT,
not first actionable entries. Safe aggregate status is `opening_status`.

Five new append-only tables publish the complete cycle atomically with SQL-side
first-writer wins. Every scan records exact advice, plan changes, scope and
source card/snapshot/profile/code hashes; neither reading a page nor requesting
alerts publishes advice. First pre-open and +15..+25min rosters freeze only in
their actual windows with fresh source coverage; missed windows remain missed.

One first actionable entry per ticker/session is assessed. Cached +15/+60min
reference marks must be observed within the declared following 300s window;
missed marks never use a later day. Settled closes use exactly day 0 and sessions
1/3/5, with as-of availability and matched SPY. These are directional UNDERLYING
reference returns before fees/spread/slippage, not option P&L or PAPER fills.
An old PAPER entry cannot improve the later opening reference. Later updates
remain a correlated journal, not new independent performance samples. History
is paginated and selectable by day; prior entries remain visible across days.

The home alert control uses new private `/api/acilis/uyarilar`: an entry requires
a matching durable current opening BUY. Recorded exits are retained. Original
`/api/canli/uyarilar`, existing browser defaults/seen ledger and source policy
remain compatible. Server-clock-plus-monotonic expiry withdraws open-page calls.
Legacy source-card detail is folded separately from the opening instructions.

## Judgment calls

1. Use existing frozen flow/price/news strategy as an experimental source, then
   add timing/data-quality gates. Reused daily-bar studies cannot validate this
   new intraday rule; there is no fabricated win probability or confluence score.
2. Underlying reference marks are separately labelled. Available cached quotes
   do not support an honest new option fill/P&L backtest at every opening call.
3. Physical publication occurs after reading inputs, not at scan start. Actual
   persisted source IDs and atomic keys handle overlapping workers without
   publishing an in-memory scan as recorded evidence.
4. Preserve all statements while separating first entries and frozen rosters.
   Repeated green cards cannot inflate n or replace the original morning list.
5. Keep all historical profiles, acceptance/results and forward_v2 untouched.
   The only older test edit adds the five explicit evidence-table classifications.

## Validation and release

81 dedicated cases cover clock/DST/holidays, strict stale/future/missing source
and news/option gates, held exits/conflicts, transaction rollback/concurrency,
changed plans, fixed checkpoints, first observations/entries, exact future
reference windows/dates/SPY, previous PAPER isolation, read-only history and
private routes/assets/alerts. A real legacy cycle fixture also proves structured
per-name capture without additional vendor paths.

Actual local FastAPI app with clearly synthetic LOCAL inputs: pre-open, +15 and
stale states at 390/1440px; home summary, durable alert filtering, monotonic expiry,
zero overflow, JavaScript errors or axe WCAG A/AA violations. No authenticated
production visual review or real market sample is claimed. Tailwind asset rebuilt
and CSS/new JS URLs versioned. Browser artifacts remain outside the repository.

Full local gate: **5201 passed, 52 key-gated skips**, strict mypy **271 source
files**, ruff/lock/diff clean. PR/main CI and matching Railway SHA must also pass
before reporting the release shipped. The last pre-release production check on base
4229f69 still reports startup storage OperationalError, no completed scan. Native
owner logs previously identified PostgreSQL recovery mode; there is no Railway
control-plane/database credential here, and no evidence has been removed or
infrastructure capacity purchased. HTTP health 200 is liveness only.

Next: recover the external PostgreSQL service without destroying its evidence;
then verify real opening cycles and collect prospective coverage before any
new calibration/version. No unobserved window will be backfilled as a live call.
