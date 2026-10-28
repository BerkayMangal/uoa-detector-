# Phase 5.46 — opening availability and measurement coverage

Frozen before code, 2026-10-28 Istanbul. Owner requests continued improvement
and delivery. Latest live cdc7add still fails at startup storage; actual Railway
PostgreSQL logs/control-plane access remain external. No claim of recovery,
working live recommendations or measured performance without real evidence.

## Connection visibility and useful unavailable screen

Add a pure fixed-allowlist diagnostic inspecting ONLY a native DBAPI exception
(not SQLAlchemy SQL/parameters). Recognize explicit recovery/starting/shutdown,
connection refusal, DNS, connection timeout, authentication, TLS and connection
capacity. Known SQLSTATE can support its specific meaning; 57P03 alone means
not accepting connections, not proof of recovery. Unknown/unreadable native
message yields no specific diagnosis. Only the fixed reason code and static
Turkish description/action may leave the adapter; never raw text, host, user,
password, URL, SQL, native parameters or dynamically supplied exception name.

Startup/cycle health may add a fixed `database_hint` when a cause is recognized;
existing Failure fields/categories and old health shape without a hint remain
compatible. The next real success/restart clears old evidence as existing state
replacement does. No new connection/probe/vendor calls, retry/cadence changes,
DDL reset, WAL manipulation, capacity purchase or transaction replay.

Private GET /acilis must remain useful during OperationalError: HTTP503,
BEKLE/data unavailable, actual declared exchange/Istanbul opening times, static
safe diagnosis if available and manual GET retry. No positions, statements,
zero P&L, fake journal rows or old AL/SAT inferred during an unavailable read.
This fallback does not start notification polling or automatic timed refresh.
Other unexpected errors retain existing 500 behavior. Existing generic 503
read/mutation semantics and JSON API shapes stay compatible, including UNKNOWN
POST outcome and no automatic financial-form retry. Optional static diagnostics
and an opening-clock link may be added without replacing those guarantees.

## Measured evidence, not apparent calibration

Private opening desk adds a read-only summary for its bounded displayed cohort
(last existing profile history_limit first-entry records, or chosen journal day).
Include exact scope, distinct first-entry keys/session dates/tickers, and per
existing horizon: observed valid directional underlying returns, matched SPY
excess coverage, positive-return count, arithmetic means, pending target time,
awaiting an actual result record, and explicit recorded missing/invalid evidence.
Unknown coverage stays unknown; no-record-after-due is NOT assumed a price gap.
Use only as-of durable first-entry/outcome records from the current opening
profile. Deduplicate immutable primary keys; correlated later journal updates,
other policy versions and old PAPER/manual fills cannot enter its denominator.

No recommendation/profile/threshold/outcome edits, re-computation of trading
advice on HTTP, inferred win probability, Sharpe/annualized return, calibrated
confidence, independence claim or promotion to validated alpha. Means remain
underlying references before costs, NOT options P&L or executed performance.
Zero observed returns produce unknown means, not 0% success. Show session count
and the repeated/correlated nature of names; allow tiny samples to be described
without an invented universal validation threshold. Do not concatenate horizons
as independent trades. No new strategy optimization or reserved data use.

## Verification and delivery

Native-cause tests cover specific/ambiguous/unreadable errors, strict SQL/secret
isolation, startup/cycle integration and stale hint replacement. Actual private
503/401/API/mutation behavior, clocks/DST, no writes and no auto retries must be
verified with labelled local fixtures. Summary tests cover repeated keys,
different profiles, as-of/future data, no measurements, elapsed vs pending/missing
coverage, real zero/negative returns and exact matched denominators, declared
session dates/early close/holidays, no option/PAPER extrapolation.

Full hermetic pytest, strict mypy src/webapp, ruff, lock and diff clean before
each commit; preserve old test assertions and frozen research artifacts/profiles.
Actual local app mobile/desktop/axe/JS check. Green PR/main CI and matching
Railway SHA; recheck actual native reason and distinguish code delivery from the
still-external database recovery. Continued publish/merge/deploy is authorized.
