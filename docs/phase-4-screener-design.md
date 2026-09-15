# Phase 4 — UOA Screener UI (design / acceptance)

**Status: frozen on commit. Build against this.**

## What it is

A decision-support **screener**: a web dashboard that surfaces the
detector's unusual-activity signals — each with a full per-axis breakdown
and plain-English explanations — for Berkay to review with his own
trading judgment. The page is in **English**, with detailed explanations
everywhere (tooltips + glossary).

**Honest framing (shown in the UI):** the Phase 3.5/3.6 backtests found
NO proven mechanical edge in the raw signals. This is a *screener* —
"what is unusual right now" — not a proven buy signal. The UI states this
plainly so it is never mistaken for an auto-edge trader.

## Architecture

```
[Mac: ThetaTerminal + detector]              [Railway (cloud)]
 live option flow → pipeline → signals  →  Postgres DB  →  FastAPI web app
 (runs locally; data is local)    (writes)   (source of    (reads + renders;
                                              truth)         polls for live)
                                                                  ↓
                                                       Berkay → browser URL
```

Live data (ThetaData) is local to the Mac; the UI is hosted on Railway.
The detector writes signals to the Railway Postgres; the web app reads
them. With HTMX polling the dashboard is **live** from the first cut
(true SSE/WebSocket push is a later refinement).

## Tech stack

| Part | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | reuses the project's Pydantic signal models directly |
| Page | Jinja2 + HTMX + Tailwind (CDNs, no build step) | one Python service, easy explanations, HTMX = live updates |
| DB | Postgres (Railway) via SQLAlchemy | the project already uses SQLAlchemy |
| Deploy | Railway (repo-connected) | one web service + Postgres addon |

Data access is abstracted behind a `SignalRepo` so it reads SQLite for
local dev and Postgres in the cloud (`DATABASE_URL`).

## What each signal card shows (English, explained)

- Header: ticker · CALL/PUT strike/expiry · time · the **LABEL** (colored)
  · big **combined score**.
- Per-axis breakdown — each axis with its score, a one-line "what this
  means", and why it scored: UOA flow, Convexity, Dealer gamma (GEX),
  Sweep/Cluster, IV, Time-of-day.
- Print detail: premium, spot, DTE, fill_side (aggressive?), ISO (sweep?).
- "What this means" — plain-English gloss of the label_reason.
- Tooltips/glossary on every term (GEX, convexity, ISO, DTE, …).
- Filters: ticker · label (e.g. high-conviction only) · min score · today/live.

## First-cut scope (this build)

1. `src/uoa_detector/observability/postgres_writer.py` — a
   `DecisionRecordWriter` that writes signals to the DB (the live sink),
   alongside the existing NDJSON/Parquet writers.
2. `webapp/` — FastAPI app: `SignalRepo` (SQLite/Postgres), Jinja2
   templates (dashboard, signal card, glossary), Tailwind+HTMX via CDN,
   HTMX polling for live updates.
3. Railway deploy config (start command, requirements, `DATABASE_URL`).
4. Seed the DB from a recent detector run so the dashboard is non-empty on
   first open; then wire the live detector to write continuously.

Web dependencies live in an optional `web` dependency group so the
backtest tool is not burdened.

## Done when

- The dashboard renders real signals with the per-axis breakdown +
  explanations, runs locally (uvicorn) and is deployable to Railway, and
  updates as new signals arrive (polling).
- Existing tests stay green; the web app has its own tests (repo + routes).
- The screener-not-edge framing is visible in the UI.

## Not in this cut

- True WebSocket/SSE push (polling suffices for v1).
- Auth / multi-user (single-user, Berkay).
- Any trade execution (decision-support only — manual, his broker).
