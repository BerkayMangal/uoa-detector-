# Vendor historical controls, v1

Immutable original derived diagnostic and independent reconciliation. No raw
vendor payload, authentication or database credentials are included.

- Source: NFLX, AAPL, NVDA and matched SPY; 2026-06-24–2026-09-18.
- 61 source sessions, 20 warmup, 41 evaluation sessions. Exploratory reuse.
- 12 fixed flow/underlying reference controls, 3,000 descriptive shared timing
  resamples. No selected winner, full 438-candidate calibration or live promotion.
- Historical availability is assumed from native alert aggregation end plus
  declared lag. No physical historical receipt or original LIVE/READY replay.
- Source packet identities/hashes are retained in report.json. Raw packets
  reside outside the checkout and are required for independent re-audit.
- report.json is the unchanged first return calculation; audit.json independently
  checks its 1,024 reference units and attributes the same fixed units' gross
  move and modeled two-sided price costs. Units are not a cash portfolio.
- Dated option chains were captured for provenance, not used for option P&L.
  Daily chain snapshots do not establish executable historical quotes.

Reproduce on the sealed private archive:

```sh
uv run python -m scripts.audit_vendor_history --source /path/to/private/archive \
  --diagnostic artifacts/vendor-history-v1/report.json --output /tmp/new-audit.json
```

The audit fails if source prices/hashes, original calculations, profiles or
unit arithmetic differ. The web reader verifies both derived artifacts and
calculation dependencies before display. See docs/phase-5.51-results.md for
figures, limits and contracts. Do not overwrite these first results to tune a
new hypothesis; register subsequent research separately.
