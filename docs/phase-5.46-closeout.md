# Phase 5.46 closeout — availability and measurement coverage

KAPALI for implementation; actual PostgreSQL recovery remains OPEN/external.
The current pre-release production cdc7add still fails during startup storage.
A native diagnostic or useful 503 page is not a repaired database or live alpha.

## Delivered

- A pure native DBAPI adapter returns fixed connection reason codes and static
  Turkish copy. Explicit recovery, starting/shutdown, refusal, DNS, connection
  timeout, authentication, TLS, capacity and lost connections are distinguished.
  57P03 alone stays ambiguous (`not_accepting`). SQLAlchemy wrappers/SQL/params,
  unreadable detail and unknown messages cannot produce a specific diagnosis.
  Raw exception content never leaves this adapter.
- Recognized startup/cycle causes add `live_alpha.database_hint`. Existing Failure
  fields/categories and the no-hint health shape remain intact. The next restart
  or successful cycle clears the hint through existing whole-state replacement.
  No probes, vendor requests, connection target changes or retry changes.
- Private `/acilis` returns an actual 503 with today's exchange/Istanbul clock,
  BEKLE, unknown measurement state, known static diagnosis and a manual GET link
  when storage fails. No alerts, scripts, fake empty portfolio/zero return,
  automatic refresh or financial-form replay. A render failure still returns a
  secret-free 503 with the clock. Generic outage/API/UNKNOWN POST semantics and
  unrelated 500 behavior are preserved. Operator guide extended with the actual
  recovery evidence, native-code limits and correct PostgreSQL service-log path.
- The opening page reports the displayed bounded first-entry cohort: current
  profile only, immutable first-entry keys, dates/tickers/session counts and
  each existing horizon's valid directional underlying/matched SPY sample sizes,
  arithmetic means and positive-reference counts. Pending target, due-but-no-
  record, partial benchmark, recorded missing, invalid and unknown calendar are
  separate. Empty observations show unknown, not 0% success. All statements and
  old PAPER/manual fills remain outside this denominator.
- On mobile the means and their denominators are visible together in three
  columns. Statistics are observational references before costs, not option P&L,
  executed returns, confidence, independent trades or a calibrated edge. No
  policy thresholds, profiles, outcomes or historical research were changed.

## Judgment calls

1. Native message detail may be more precise than its SQLSTATE, but an unknown
   message does not justify guessing a cause. Only fixed copy/code is public.
2. Keep the unavailable opening screen genuinely unavailable (503); the declared
   clock is safe to calculate without market data. Reading it publishes nothing.
3. A missing outcome after its due time means awaiting a recorded result; it is
   not evidence that the underlying price was unavailable. Only actual recorded
   missing evidence enters the missing category.
4. Underlying means and matched excess means use their own explicit denominators.
   Partial SPY coverage cannot silently change either into an apples-to-apples
   sample. Repeated first-entry keys, other profiles and future data are excluded.
5. Use the existing profile's history limit and declared horizons/calendar; no
   new sample-size cutoff, optimizer or maturity/policy threshold is invented.

## Verification and delivery

**72 new cases**: safe native message/code recognition, ambiguity, unreadable and
nested wrappers, real psycopg exception classes without a connection; health
startup/cycle insertion and clearing; actual 503/401/API/POST/render-failure/DST/
holiday/no-polling behavior; exact current-profile first-entry dedup/limits,
zero/negative returns, separate matched denominators, empty/future/invalid marks,
pending vs missing, calendar/DST/early close, real append-only ledger GET with
SQL write capture, no option/PAPER extrapolation.

Full hermetic gate: **5273 passed, 52 key-gated skips**, strict mypy **273 source
files**, ruff/lock/diff clean. Existing assertions and frozen acceptance/profiles/
research outcomes unchanged. Generated Tailwind CSS rebuilt/cache URL versioned.
Actual local FastAPI with labelled synthetic LOCAL input: pending and measured
references plus recovery outage at 390/1440px. No overflow, JS errors or axe WCAG
A/AA issues; no alerts/scripts/advice on 503. Artifacts remain outside git. No
production authenticated visual review or real forward samples are claimed.

Delivery commit Phase 5.46.1; green PR/main CI and actual matching Railway SHA
required before announcing shipped. The last pre-release startup failure has no
SQLSTATE; the owner's earlier native log reported recovery mode. No control-
plane/database credential is available here and the requested PostgreSQL service
log is still pending. No database evidence was removed, no WAL operation or paid
resource change performed. Completion requires actual successful scan/desk/
research/opening cycles and recorded coverage, beyond HTTP liveness or deploy.

Next: use the observed native reason and PostgreSQL service's own logs to resolve
the external connection/recovery incident; then collect real prospective coverage
without fabricating missed opening lists or retroactive live recommendations.
