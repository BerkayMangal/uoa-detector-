# Fusion research v1 — registered exploratory reuse

Actual run:70names ×252 daily sessions,17,640 OHLCV rows,2025-09-18–2026-09-18.
These prices were already searched. Reserved StudyG/post-cutoff prices were not
opened. No live rule, old profile, PAPER record or prior result was changed.

`plan.json` was written and fsynced before the first new return calculation. It
binds the source/profile/code, all5,184 candidate keys, the8192 cap/900s budget
and whole-date splits with5-session purge plus1-session embargo. The grid is
6families ×4lookbacks ×4thresholds ×3volume gates ×3regimes ×2directions ×3daily
horizons. Zero/inapplicable/missing states are retained rather than discarded.

-5,184 actual strategy configurations;36,298 reference replay evaluations,
 including repeated train/validation aggregation and fixed selected controls.
 These are not36,298 independent strategies or billions of trials.
-1,728 cached rosters;258 zero-selection configurations;0 missing training cells.
-200 joint five-session block resamples share dates across the entire family;
 4,450 unique validation return vectors. This is descriptive reused-history
 search accounting, not a p-value for adaptive selection or verified alpha.
-5.752seconds measured search wall time in this environment; no vendor call.

Each fold selects using training only. Its selected validation net totals are
+1,583.83 / -9,772.71 / +2,129.67USD: unstable. The final training-selected daily
short reference recipe has172 resolved units, net+560.65USD and same-direction
matched-SPY excess+1,371.16USD in the final diagnostic. Removing its best trade
turns net into-70.12USD; removing its best three gives-1,253.68USD. At10bp/side
net is+131.33USD; at20bp it is-707.22USD. All67 names in the last-fold ticker
support diagnostic fall back to the global estimate; none met independent
60/20 observation and20/10 day support. No calibrated success probability exists.

These are independent2500USD reference units with at most4new units/day, not a
cash/margin-constrained portfolio. Short mirrors exclude borrowing, locate,
dividend, margin and fill-availability effects. Daily1/3/5-session labels are
separate from15/60-minute prospective paired last-trade observations. Historical
semantic-news/PIT-fundamental/event-time-flow features are absent, not zero.

**winner=null; inference=NOT_READY; live_promotion=false; validated_alpha=false.**
At least80 genuinely eligible prospective sessions and complete labels remain
required. Nothing here promotes the short recipe to live advice.

`grid.jsonl.gz` retains every candidate/fold outcome. `report.json` contains the
stored summary, final selected ledger, controls, joint diagnostic and identities.
`audit.json` independently reconstructs all172 final units directly from raw
CSV prices, the causal top-four roster, quantities, fees, SPY references and
best-trade removals. It also checks the5,184-key registry. It does not independently
reconstruct every candidate/fold or establish prospective performance. The
standalone auditor remains in the session evidence directory, with its SHA
recorded in the audit. Raw API payloads and credentials are not in this folder.

Reproduce as a **new** immutable run, never overwrite this first result:

```sh
OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 uv run python scripts/fusion_research.py \
  --source data/study_f/bars.csv --output /tmp/fusion-research-reproduction
```

Compare data/profile/code identities before treating a rerun as equivalent.
