# UOA + Convexity Detector v5 — Phase 1 Scaffold

Foundational scaffold for a multi-module options-flow signal-detection system,
built to the v5 spec (`UOA_Convexity_Detector_v5.docx`). This is **Phase 1**:
project skeleton, core domain types, the pluggable data-source interface, the
scoring engine, the labeler, the risk sizer, and an end-to-end smoke test
driven by synthetic data. Individual enrichment modules (Modules 21–28, 34–39)
are stubs in this phase; real implementations land in Phase 2.

---

## Quick start

```bash
# Install dependencies (creates .venv automatically)
uv sync

# Run the synthetic smoke scenario through the full pipeline
uv run python -m uoa_detector run --source synthetic --scenario default

# Run all tests
uv run pytest -q

# Lint
uv run ruff check .

# Type-check
uv run mypy --strict src/
```

The default synthetic scenario emits 8 prints that exercise these labels:
`IGNORE_NOISE`, `CONVEXITY_WATCH`, `CONVEXITY_CLUSTER`, `STANDARD_UOA`,
`SWEEP_UOA`, `PENALIZED_BELOW_THRESHOLD`, `LEAP_POSITIONING`,
`HIGH_CONVICTION_SEQUENCE`. Each prints one structured log line with
timestamp, ticker, label, combined score, and max-R.

---

## Repository layout

```
src/uoa_detector/
├── domain/             # Pydantic models — pure data, no behavior
│   ├── events.py       #   OptionsPrint, EnrichedEvent, AppliedPenalty
│   ├── scores.py       #   SubScores, CombinedScore
│   ├── labels.py       #   SignalLabel (17-value StrEnum), LabelDecision
│   └── risk.py         #   RiskBucket, PositionSize
├── sources/            # Pluggable flow feeds
│   ├── base.py         #   FlowDataSource Protocol — the only entry point
│   ├── synthetic.py    #   SyntheticFlowSource — scriptable in-memory source
│   └── scenarios.py    #   Default scenario builder for the CLI/tests
├── pipeline/
│   ├── orchestrator.py #   Pipeline runner — source → stages → score → label → size → store
│   ├── stage.py        #   EnrichmentStage Protocol, PipelineContext
│   └── stages/         #   12 enrichment stages (M21–M28, M34/35/37/38/39)
├── scoring/
│   ├── combined.py     #   v5 combined-score formula (FULL implementation)
│   ├── early.py        #   early-convexity score (FULL implementation)
│   └── penalties.py    #   Module 36 penalty engine (FULL — all 8 conditions)
├── labeling/
│   └── labeler.py      #   17-label decision tree (FULL implementation)
├── risk/
│   └── sizer.py        #   Part 5 risk sizer with HCS scale-in (FULL)
├── backtest/
│   └── store.py        #   In-memory store matching Module 29 schema
├── config.py           #   pydantic-settings config — every v5 number lives here
├── errors.py           #   Typed exceptions
└── cli.py              #   `python -m uoa_detector run …` entry point

tests/
├── unit/               # Per-component unit tests (5 files)
└── integration/        # End-to-end pipeline test
```

---

## Adding a new data source

The pipeline only knows about the `FlowDataSource` Protocol declared in
`src/uoa_detector/sources/base.py`:

```python
from collections.abc import AsyncIterator
from typing import Protocol

class FlowDataSource(Protocol):
    def stream(self) -> AsyncIterator[OptionsPrint]: ...
    async def close(self) -> None: ...
```

To plug in a real feed (Polygon, Unusual Whales, IBKR, CSV replay, …):

1. Create `src/uoa_detector/sources/<your_source>.py`.
2. Implement a class with `stream` (async generator yielding `OptionsPrint`)
   and `close`. **No inheritance is needed** — the Protocol is structural.
3. Normalize each upstream event to an `OptionsPrint` (see
   `domain/events.py` for the required fields). Use `Decimal` for
   prices/premiums; floats are reserved for scores and IV.
4. Raise `DataSourceError` (or a subclass — see `errors.py`) on feed failures
   instead of bare `Exception`.
5. Register the source name in `cli.py` so `--source <your_source>` resolves.

`SyntheticFlowSource` in `sources/synthetic.py` is the reference
implementation and is used by every test.

---

## Architecture flow

```
OptionsPrint  →  EnrichmentStage*  →  PenaltyEngine  →  ScoringEngine
                                                              ↓
                       BacktestStore  ←  RiskSizer  ←  Labeler
```

Stages run in `default_stage_pipeline()` order
(`pipeline/stages/__init__.py`). Each stage mutates a shared `EnrichedEvent`
in place and is required to be idempotent. After all stages run, the
penalty engine populates `event.applied_penalties`, the scoring engine
computes `combined_score_pre_penalty` and `combined_score_post_penalty`
(applying the DTE multiplier from Module 35 to convexity and gamma only),
the labeler picks one of the 17 v5 labels, and the risk sizer maps that
label to a `PositionSize`. The result is persisted to the in-memory
`BacktestStore` whose schema matches Module 29.

---

## Phase plan

**Phase 1 (this delivery)** — scaffold + 4 fully-implemented engines:
- Project skeleton, domain types, Pydantic config
- `FlowDataSource` Protocol + `SyntheticFlowSource`
- Pipeline orchestrator + 12 stub stages (with `# TODO(phase-2)` markers)
- Scoring engine (combined + early-convexity)
- Module 36 penalty engine (all 8 conditions, exact v5 values)
- 17-label decision tree
- Risk sizer including HCS scale-in
- In-memory backtest store matching Module 29 schema
- Module 39 time-of-day stage (cheap; rest of system depends on it)
- CLI smoke run + full unit + integration tests

**Phase 2** — replace the stubs with real logic:
- Module 21 dealer-gamma overlay (compute gamma exposure, flag
  `GAMMA_ACCELERATION_RISK`)
- Module 22 event-calendar overlay (earnings/FDA/Fed/M&A score)
- Module 23 price confirmation (VWAP, HH/HL, prior-day breaks)
- Module 24 IV exhaustion filter
- Module 25 sector/peer confirmation (using `PipelineContext.sector_map`)
- Module 26 dark-pool tape confirmation
- Module 27 opening-vs-closing estimator
- Module 34 sweep/block classifier (multi-venue ms-window detection)
- Module 37 relative-premium normalization (vs ticker 30-day median)
- Module 38 temporal clustering with the 60-min window + 90-min decay watcher
- Module 28 next-day OI validation scheduler

**Phase 3+** — production concerns:
- Real adapters: Polygon, Unusual Whales, IBKR, CSV replay
- Postgres / TimescaleDB persistence (drop-in replacement for `BacktestStore`)
- Web API + dashboard for live signal monitoring
- Live broker execution layer
- Outcome backfill (1h/1d/3d/5d/10d returns, IV change, MFE/MAE) for the
  backtest schema fields that are `None` in Phase 1

---

## Phase 1 spec ambiguities (flagged, not silently resolved)

1. **Contradiction penalty appears twice in the spec.** Part 3's combined-score
   formula has `− contradiction_penalty`; Module 36's penalty table also
   lists "Flow direction contradicts price action: −0.15". Same condition,
   same value. **Phase 1 counts it once** (through the Module 36 list, with
   `event.contradiction_penalty_applied` set for telemetry). Alternative
   reading: stack to −0.30 when contradiction fires.
2. **Label precedence below the four explicit gates** is not enumerated in
   the spec. The order encoded in `labeling/labeler.py` is derived from the
   risk-bucket hierarchy and Part 7 Detection Sequence:
   `HCS → CONFIRMED_OPENING → PRE_CATALYST → SWEEP_UOA → STANDARD_UOA →
   OPTIONS_EQUITY_TAPE → SECTOR_FLOW → GAMMA_ACCEL → CONVEXITY_BURST →
   CONVEXITY_CLUSTER → OPENING_UNCONFIRMED → CONVEXITY_WATCH → IGNORE_NOISE`.
3. **DTE = calendar days**, not trading days, per the task instruction.
4. **`ScenarioOverrideStage` in `sources/scenarios.py`** is a Phase-1-only
   harness that pre-populates sub-scores the Phase 1 stub stages can't yet
   derive. Phase 2 deletes it.

---

## Conventions

- Python 3.11+ required (StrEnum, `datetime.UTC`, `zoneinfo`)
- `decimal.Decimal` for all prices and premiums; `float` only for scores and IV
- All datetimes are tz-aware UTC internally; conversion to America/New_York
  happens only at display boundaries (Module 39's time-of-day lookup)
- `typing.Protocol` for pluggable interfaces (no ABCs)
- `pydantic` v2 for every domain model and config
- Typed exceptions in `errors.py`; never catch broad `Exception`
- No global state — pass dependencies explicitly via `PipelineContext`
- Tests via `pytest` + `pytest-asyncio`
