# Phase 5.53 — physically observed news and fixed policy ablations

Code verified; exact PR/main CI, native deployment and receipt counts are recorded
in the release PR after activation. No calibrated winner or validated alpha.

## Delivery

Existing UW news requests now have a failure-isolated physical response recorder:
private compressed packets, checksums, exact application-check identity and
post-packet-commit receipts. Same original client call/cadence/quota/return value;
no extra vendor request. Cached news cannot reuse a different check's packet,
including after a failed research write and restart. Bounded asynchronous writes
retain their lock until actual completion; no retry queue or thread backlog.

New scans include machine-readable price context for independent research only.
The read-only adapter uses completed-scan knowledge times, refuses partial first
sessions and truncated/conflicting exports, and never supplies strict labels from
legacy hook-start price clocks or explanatory text. News packet/provenance,
snapshot, gates, price, profile and implementation identities remain auditable.

Five fixed arms compare base flow+price, relative strength, news, AND and OR on
one first-opportunity/four-slot roster. Primary E-minus-B uses shared complete
daily ledgers and identical outlays; unknowns never become false or zero P&L.
Cached-price reference units use a one-minute delay, 60-minute hold, exact-time
SPY and whole shares with 5bp/side; 10/20/50bp stresses are diagnostic. An
80-complete-common-session gate does not itself establish alpha. No fitted
winner, significance claim, annualized trade-count Sharpe or option fill.

Private `/arastirma` and `/api/arastirma/haber` expose safe bounded readiness
counts, not raw packets or an optimizer. CLI `python -m scripts.news_research`
provides inspect/export/evaluate with aware bounds, immutable outputs and reserved
path refusal. Use `--since` at/before the first session assessment or at/after its
close; export rejects partial sessions and size truncation. Data can be exported
in bounded complete-session intervals; no silent partial-universe performance.

## Actual evidence and verification

- Final pre-commit gate: **5,619 passed / 52 key-gated skipped**, two existing
  deprecation warnings, 296.46s. Strict mypy clean on312 files; ruff and lock clean.
  Baseline5,502; total additional collected cases117, including new tables'
  generic durability coverage. Prior assertions preserved; P37 classification
  only adds both immutable news tables to the protected set.
- Existing local archive:614 market packets/61 sessions, zero news packets.
  Local dev DB has no strict news storage and zero scan/roster sessions. Actual
  inspect/evaluate CLI returned STORAGE_UNAVAILABLE / INSUFFICIENT_EVIDENCE,
  inference NOT_PERFORMED and winner null. This validates the command path, not
  a market backtest. New market replays and fitted trials executed: **zero**.
- Chromium at1440/390/320: no page overflow, all information disclosures open,
  unavailable storage and pending integrations explicitly labelled. Private
  evidence `/workspace/uoa-design-review/news53-*` uses local/synthetic readiness,
  not invented market performance.
- Original live/opening/event-calibration/alpha-lab policies and research reports
  are unchanged. Study G remains sealed. No broker, paid plan or model call added.

## Judgment calls and source integration

1. Physical receipts and exact check binding instead of retroactively blessing
   old application timestamps.
2. Independent common-roster policy comparison, not a purported exact live replay
   or randomized causal news intervention.
3. Session-safe export and completed-scan price knowledge; source/cache clocks
   remain distinct from exchange execution time.
4. Presence is response-level news availability, not positive sentiment, surprise
   or exhaustive media coverage. Unknown and failed capture remain explicit.
5. Correct the owner-supplied brief's OR/factorial, power, placebo, H1 stopping,
   Sharpe and holdout claims in `phase-5.53-research-design-review.md`.
6. Read-only source audits identify Mahalle corporate/event cards and Stajyer
   macro/note sources; dedicated context-only feed draft PRs are separate work.
   No curated absence becomes negative news evidence and no duplicate story
   becomes independent corroboration. See `phase-5.53-shared-news-sources.md`.

Next: validate native physical capture, accumulate genuinely future complete
sessions, then inspect the registered descriptive contrasts. Direction/novelty,
earnings, normalized flow and original-call parity remain separately registered
work. The two new app hostnames require environment draft review/publication;
their feed endpoints and credentials are not claimed deployed or connected here.
