# Event calibration v1 receipts

These are readiness receipts, not a positive strategy result. 438 candidates
are preregistered; ZERO real market replays were possible with local event data.
No winner, fitted win probability or live policy change.

readiness.json counts the actual registered local evidence. capability-current.json
records a zero-request missing-credential attempt. observed-calibration.json is
the actual DATA_BLOCKED outcome from the empty local development archive.
Synthetic software fixtures live only in tests, not in these receipts.

Contract: docs/phase-5.50-event-calibration-acceptance.md.
Findings/limitations: docs/phase-5.50-results.md.

Commands run from the repository root with uv run python -m scripts.event_calibration:

- inventory --output <new-readiness-file.json>
- export --sqlite <existing-read-only-db> --output <new-private-events.json>
- export --since <aware-ISO-datetime> --tickers NFLX,AAPL --output <new-private-events.json>
  uses a securely bound DATABASE_URL; never paste the URL into a command or chat.
- probe --ticker NFLX --days <completed-session-A> <completed-session-B> --output <new-probe.json>
  uses securely bound UNUSUAL_WHALES_API_KEY, at most eight requests; no retries.
- calibrate --input <sealed-private-events.json> --output <new-report.json>

Every output path must be new. Keep real exported private archives outside git.
The loader checks the export manifest hash and rejects reserved-study paths.
Data-blocked calibration/probe commands return exit code 2 intentionally.

quote-cost-audit.json is a separate real descriptive calculation of 4,442 saved
quote combinations, frozen before calculation in the quote-cost addendum.
It is not a return backtest or an optimal cutoff. Reproduce with
uv run python -m scripts.audit_event_quote_costs --output <new-file.json>.
The source cashflow reconciliation proof is retained outside the repository.
