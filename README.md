# UOA + Convexity Detector v5

**What this is:** decision support for options-flow screening.
- It surfaces unusual options activity and scores it against structural
  context: dealer-gamma regime, IV rank, sector/peer flow, dark-pool prints,
  open interest and the event calendar.
- A human reads the output and decides.
- Nothing executes: there is no broker link and no auto-trading. Trade entry
  is always a manual decision through the operator's own broker.

**What it has not found:** a tradeable edge. The project's canonical
statement (`docs/INDEX.md` §0):

> No tradeable edge found. Directional UOA confluence (UW-free, v6):
> REJECTED (phase-3.6-closeout). Gamma-regime directional and pinning:
> REJECTED (4.9, 4.11). Vol premium: untradeable after costs
> (edge_to_money.md). Conditioning replication: WEAK (study_D_result.md).
> UW-fed Track B (v5_gamma_squeeze with real UW enrichment) has never been
> testable, because UW history is about 7 days (phase-3.5.5-status B1).

**The +0.77 Sharpe figure is superseded.** Earlier versions of this README
called the "long-gamma + high IV rank beats blind vol-selling by +0.77
Sharpe" conditioning read validated. That figure was in-sample, on the burned
2025-05 → 2026-04 panel.

Its pre-registered out-of-sample test is Study D (`docs/preregister_D.md`,
`docs/study_D_result.md`):
- one run, no re-tuning;
- fresh window 2024-05-01 → 2025-04-30, the same 24 tickers;
- statistic: vol-points VRP Sharpe spread, with no option priced and no
  costs.

The result is **WEAK / BORDERLINE**:

| Causal metric | In-sample (burned panel) | Fresh 2024 window |
|---|---|---|
| Conditioned − unconditioned Sharpe spread | +0.51 | +0.22 |
| Welch t (conditioned − unconditioned) | +1.88 | +1.47 (not significant, < 2) |

The conditioning's incremental value over blind vol-selling is not
statistically significant and shrank out of sample.

---

## What is in the repo

| Part | What it does | Entry point |
|---|---|---|
| Detection pipeline | `OptionsPrint` → enrichment stages (M21–M27, M34, M35, M37–M39) → M36 penalties → combined score → label → risk bucket. M28 runs as an overnight validator. | `src/uoa_detector/` |
| CLI screener | A ranked digest of Unusual Whales flow for the listed tickers (latest session by default), enriched by the live-verified Phase 3.9 UW providers | `uoa-detector screener --source rest` |
| Web app | FastAPI dashboard on Railway: stored and live signals, a gamma / vol board and a journal of manually logged trades. A live worker polls UW flow during market hours. | `webapp/` |
| Backtest | Store, parquet replay, 4-cell runner with two engines (`--trades historical` / `replay`, verdicts not comparable), falsification verdict | `uoa-detector backtest run-4cell` |
| Research | Pre-registered studies and their records | `scripts/`, `docs/` |

Where to read next: `docs/INDEX.md` (current truth, doc map, burned data
windows, Phase 5.x registry), `docs/DATA_INTEGRATION.md` (credentials,
Railway runtime, environment variables), `docs/BACKTEST.md`,
`docs/MODULES.md`.

## Quick start

```bash
# Install dependencies (creates .venv)
uv sync

# Synthetic scenario through the full pipeline (no credentials)
uv run python -m uoa_detector run --source synthetic --scenario default

# CLI screener on live Unusual Whales REST flow (needs UNUSUAL_WHALES_API_KEY);
# writes reports/screener_<YYYY-MM-DD>.md
uv run python -m uoa_detector screener --source rest --live-tickers SPY,AAPL

# Web app locally (needs WEB_AUTH_USER and WEB_AUTH_PASSWORD;
# see docs/DATA_INTEGRATION.md §8-§9)
uv run uvicorn webapp.main:app
```

**Gate** (run before every commit; CI runs it on every PR):

```bash
env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q \
  && uv run mypy --strict src/ webapp/ && uv run ruff check . && uv lock --check
```

---

> **Historical note.** The sections below are the original **Phase 1 scaffold**
> architecture reference (synthetic-data skeleton; the enrichment modules were
> stubs at the time). The enrichment modules (M21–M28, M34–M39) and the live
> Phase 4 screener have since been built — this document is kept as the
> architecture map, not a current status report. For current state see
> `docs/INDEX.md`.

Foundational scaffold for a multi-module options-flow detection system, built to
the v5 spec (`UOA_Convexity_Detector_v5.docx`). This is **Phase 1**: project
skeleton, core domain types, the pluggable data-source interface, the scoring
engine, the labeler, the risk sizer, and an end-to-end smoke test driven by
synthetic data. Individual enrichment modules (Modules 21–28, 34–39) are stubs
in this phase; real implementations land in Phase 2.

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
- Web API + dashboard for discretionary screening / context (decision support)
- **No broker execution layer.** Entry is always a manual decision through the
  operator's own broker; the system never auto-trades (see the positioning
  banner at the top). The vol-premium edge was validated and rejected as
  untradeable (`docs/edge_to_money.md`), so there is no mechanical signal to
  execute.
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
