# Tournament v1 — exploratory, no validated alpha

Protocol: `docs/phase-5.35-strategy-tournament-acceptance.md`.
Actual results/limits: `docs/phase-5.35-closeout.md`.

Six families / 7,776 unique configurations / 46,686 portfolio evaluations.
Validation-selected overall winner FAILED its later internal historical test.
One separately selected family permits experimental prospective PAPER only.
No family clears the joint best-of-grid search diagnostic. All history is reused.

`report.json` stores selected ledgers, equity paths, resampling draws and hashes.
`grid.jsonl.gz` stores every grid score, hash/row count referenced by the report.
`data-inventory.json` records available/excluded files and actual access limits.
Synthetic data and Study G prices are not research inputs.

Reproduce into a new empty directory; outputs refuse overwrite:

```
uv run python scripts/run_strategy_tournament.py /tmp/tournament-reproduction/report.json
```

Future hypotheses use existing cached APIs, not a retrospective API calibration.
No broker execution, automatic live-policy promotion, or rewrite of old results.
