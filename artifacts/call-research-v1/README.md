# Call research v1

Exploratory daily stock proxies on the already-researched Study F panel.
No live promotion, intraday replay, NFLX backtest or historical option P&L.

- `report.json`: integrity-checked compact results for all 144 frozen portfolios.
- `replays.jsonl.gz`: complete trade/equity records, one JSON line per replay.
- `manifest.json`: hashes and disclosed replay/diagnostic counts.

Protocol: `docs/phase-5.49-call-research-acceptance.md` and
`profiles/call_research_v1.yaml`. Interpretation: `docs/phase-5.49-results.md`.
Reproduce into an EMPTY directory with
`uv run python -m scripts.replay_call_research --output <empty-directory>`.
The existing artifact is never overwritten. No optimizer runs from a web page.
