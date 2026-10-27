# Calibration v1 — frozen exploratory run

Contract: `docs/phase-5.34-calibration-acceptance.md`.
Results/limits: `docs/phase-5.34-closeout.md`.

`report.json` contains every grid score and the selected candidate's internal
historical test. `selected-ledger.json` replays the same selected candidate's
train/validation/test ledgers and verifies exact equality with the stored metrics.
These are reused Study F prices, not untouched validation and not measured alpha.
Do not overwrite or retune this artifact. The runner refuses an existing output:

```
uv run python scripts/calibrate_desk.py data/study_f/bars.csv /tmp/calibration-v1-reproduction.json
```

The production monitor verifies report, profile and implementation hashes before
using the selected candidate, and only records future, pre-open instructions.
No Study G data, broker link or live recommendation promotion.
