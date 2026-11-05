# Phase5.61 / 5.61.1 — downside entry delivery

Implementation closed; publication requires the exact green feature/main CI,
normal manual merge and native Railway SHA/SUCCESS/healthy scan. Release hashes,
deployment identity and post-deploy proof are recorded in the release PR.

## Change

Existing P4 bearish flow/negative-price confirmation and priced PUT/debit-put
opening policy already existed. The browser entry allowlist suppressed these
instruments, and the automatic SOURCE PAPER selector accepted only BUY. This
release completes both paths without changing scoring, profiles, opening Python,
frozen research identities or earlier records.

- Current entry cards distinguish PUT AL / PUT SPREAD AL and explain the downside
  target/stop orientation separately from closing an existing position.
- The single existing notification controller supports all directions, CALL-only,
  PUT-only and upward-only entry scopes; exit selection remains independent.
- Existing CALL-only preferences stay CALL-only. Already-enabled legacy all-scope
  preferences baseline only previously ignored PUT entries before version2
  delivery, preserving fresh upward entries and PUT exit reviews. No automatic
  permission request, alert enabling or replay of old entries.
- Future READY BEARISH_SETUP source opportunities enter log-only SOURCE PAPER
  only with an existing priced positive-lot PUT structure. Downside plan JSON is
  tagged `auto_paper_scope=downside_source_v1`; ordinary upward JSON is unchanged.
  Existing opportunity uniqueness, later-fill and frozen-leg pricing remain.

## Verification

- Full unchanged hermetic gate: **6156 passed, 52 key-gated skips, 2 existing
  warnings**, 163.32 seconds; strict mypy clean on335 source files; Ruff and lock
  consistency clean; whitespace check clean.
- **27 new Python cases** exercise actual bearish source/priced structures,
  opening publication/canonical feed, source PAPER selection and exclusion,
  auth/read-only GET, stale/future/unpriced/cost/stop guards and deduplication.
  Both PUT structures continue through actual pending→open→target-close tracking;
  positive synthetic P&L is calculated from option premiums and model costs,
  not by applying a stock-short return sign.
- Existing browser assertions retained; **37 Node cases** pass, including14 new
  cases for both PUT types, exact scope matrix, invalid orientation/expiry,
  existing preference migration, independent exits and cross-tab races.
- **9 Chromium fixture cases** pass at1440/390/320 pixels across home, opening
  and scorecards: visible PUT explanation, four scope choices, retained15-link
  private navigation and no horizontal page overflow.

## Judgment calls and limits

1. Buying a PUT expresses a downside thesis; it does not instruct an existing
   holder to close stock, short shares or write naked options. There is no broker
   connection or order execution.
2. Use the existing canonical opening feed, not a second alert publisher; keep
   durable claims, expiry and failure behavior. The JavaScript version is5.61;
   open pages need a refresh. The stylesheet is unchanged at5.59.
3. SOURCE PAPER and timed opening-first-call measurements are separate cohorts.
   This is not a new15/30/60-minute option-return experiment, an optimal holding
   period result or evidence of profitable calibration. No production PUT signal
   is manufactured for release proof; actual opportunity availability depends on
   live source conditions.
4. Synthetic browser notification tests establish controller behavior, not
   delivery to the owner's particular desktop or operating-system settings.

Next: observe genuinely new downside plans and keep their instrument/source
identity in forward measurement, without retuning against these engineering
fixtures or repairing historical missing observations.
