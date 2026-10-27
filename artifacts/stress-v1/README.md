# Fixed-candidate stress audit v1

All six previously frozen tournament family winners; no new selection. Same
already inspected Study F final internal test, 2026-07-08..2026-09-18. Exploratory
robustness diagnostics only, no claim of untouched evidence or validated alpha.

- `report.json`: 504 replays, six exact baseline reconciliations, delay/cost/name/
  signal-month summaries and concentration accounting; canonical SHA in report.
- `replays.jsonl.gz`: ALL 504 full ledgers/equity/daily-excess paths, deterministic
  gzip, SHA/count in summary. Includes original and deferred signal dates.
- Frozen contract: `docs/phase-5.38-stress-audit-acceptance.md`.
- Offline reproduction: `uv run python scripts/run_strategy_stress.py
  /tmp/new-stress-directory/report.json` from repository root. Refuses overwrite.

Removing a name or signal month reruns the cash/slot allocation. Removing the
best 1/3 winning trades is separately labelled fixed-ledger arithmetic. Delay is
additional exchange sessions with frozen original ranking/ATR and unchanged
holding duration from the delayed entry; the fixed test end truncates samples.
Slippage is per side, commissions/taxes excluded. Daily OHLCV does not establish
open liquidity or actual fills. Old verdicts, live policy and prospective arms
remain unchanged, regardless of which delayed/excluded curve looks better.
