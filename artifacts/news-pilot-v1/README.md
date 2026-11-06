# Actual prospective news pilot — November4/5,2026

These are the unchanged five-arm experiment's actual derived daily aggregates,
not a fitted strategy, profitable calibration or option return backtest.

286 completed scans yield3651 candidate records (not independent trades) and8
fixed roster slots. All8 lack complete entry/exit reference marks:3 missing
entry prices and5 missing exit prices. Zero priced units,zero complete common
sessions andzero E/B paired days; net P&L/excess remain null. Six base gates are
unknown, two known true; the first uncertain roster members are not replaced
after observing later conditions. No winner or inference is performed.

Each day evaluates A/B/C/D/E under the fixed5/10/20/50bp-per-side costs. These
40 arm/cost summaries reuse2 sessions; they are not40 independent experiments.
The combined diagnostic is an additional view of those same two days. The older
15 first calls, SOURCE PAPER and new timed captures remain distinct populations.

Source dataset identities:

- November4:5d43d6036d659b8ffb50b3eb5ec96e0c414f26bd199830b7edfb50623db76eb4
- November5:d66a18712f0c571b37abeed0064af319532e633b2ef12f446421d5180006d66b

Raw exports and full reports stay private outside git. `seed.json` contains only
derived aggregates and source/export/report/audit hashes; `seed.sha256` binds
the exact bytes. Original profile/implementation identities and day bounds are
validated before importing into the new daily-pilot namespace. Import recording
time is the actual import time, never an original quote/news availability clock.

Rebuild with the private original evidence available:

```bash
uv run python -m scripts.build_news_pilot_seed --input /path/to/private/pilot --output /tmp/new-pilot/seed.json
```

The builder refuses overwrites, requires the arithmetic/state audit PASS and
checks original report/export identities. That audit reconciles64 missing
reference valuations,320 slot states and80 daily totals across individual and
combined views/costs; it does not independently reconstruct first-price choice,
news flags or roster selection. No priced valuation was available to audit.

One initial export timed out at the client's60s read boundary; the handler was
confirmed finished via a one-character-budget refusal before one longer retry.
This repeated input export is not another market trial. No vendor calls or
original record writes were performed by the pilot export/evaluation.
