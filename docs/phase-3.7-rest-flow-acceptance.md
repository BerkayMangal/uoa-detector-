# Phase 3.7 — REST-based options-flow source (acceptance contract)

Status: **FROZEN** (per D1) once code lands against it.
Owner: Berkay. Author: Claude (Opus 4.8).

---

## 1. Objective

Let the `screener` command produce the daily "which US names are worth a
look for a long-option trade today" digest using **Unusual Whales' REST
API** instead of a WebSocket.

The live WS URL (`wss://api.unusualwhales.com/v1/ws`) is wrong and 404s;
the REST endpoint `/api/option-flow/recent` is what the rest of the system
already talks to reliably (it backs the M25 sector-peer provider). For a
once-a-day snapshot a one-shot REST fetch is the right shape anyway — no
streaming loop, no reconnect machinery, no ThetaData Terminal, UW API key
only.

## 2. Honest framing (non-negotiable)

This is **decision-support**, not proven edge. Phase 3.5 (real backtest)
has not returned a verdict; nothing here claims profitability. The REST
source changes only *how flow enters the pipeline*, not the pipeline, the
scoring, or the thresholds. The digest carries the same verbatim header as
Phase 3.6 ("Decision-support candidates — ranked by confluence score …").

Ranking is by the pipeline's existing confluence (combined) score. The new
source adds **no** scoring, **no** thresholds, and touches **no**
`profiles/*.yaml` value.

## 3. Scope

1. **One shared mapper.** Refactor the row→`RawPrint` logic that lived as
   `UnusualWhalesLiveSource._map_event_to_raw_print` into a module-level
   pure function:

   ```python
   map_uw_flow_event(
       event: dict[str, object],
       *,
       source_id: str,
       source_event_id_fallback: str,
   ) -> RawPrint | None
   ```

   Both the existing WS source and the new REST source call it. WS
   behaviour is unchanged (its full test suite must still pass, D10).

2. **Field-name robustness.** The same UW flow object may name a field
   differently over REST vs WS. Known divergence: WS supplies `side`
   (ASK/BID/MID…); the recent-flow endpoint supplies `side_classification`
   (see `sources/unusual_whales/providers/sector_peer.py:186`). The mapper
   reads `side` **or** `side_classification` for the fill-side slot. See
   §5 for the exact decision and its honest limits.

   For any **required** field that is absent or unparseable, the mapper
   logs a clear ERROR that names the row's **actual** keys
   (`sorted(event.keys())`) and returns `None`. A shape mismatch is then a
   named one-line fix, never a silent drop and never a fabricated value.

3. **New source** `UnusualWhalesRestFlowSource`
   (`sources/unusual_whales/rest_flow.py`) implementing the same
   `RawFlowSource` Protocol as the WS source, but as a **one-shot**: on
   `stream()` it fetches `/api/option-flow/recent` for the configured
   tickers once, maps each row through the shared mapper, yields all
   mapped prints, then completes. It reuses the existing
   `UnusualWhalesClient` for the HTTP call (injectable for tests) and takes
   tickers + an optional lookback window.

4. **`screener --source rest`.** A new source value alongside the existing
   `synthetic` / `historical` / `live` (all left untouched). `rest` builds
   the REST flow source and runs the **same** `default_stage_pipeline()`
   and digest as the other sources. Requires `--live-tickers`. Requires
   `UNUSUAL_WHALES_API_KEY`. Does **not** touch the `run` command.

## 4. Field-name robustness decision (the load-bearing one)

- **Required fields** (mapper returns `None` + ERROR-with-keys if any is
  missing/unparseable): `ticker`, `executed_at`, `option_type`, `strike`,
  `expiry`, `premium`, `price`, `bid`, `ask`. These are the WS field
  names; the recent-flow endpoint is expected to carry the same names. If a
  name differs on REST, the mapper does **not** guess — it logs
  `row keys=[…]` at ERROR so the exact rename is a one-line fix.
- **Fill side** (optional; degrades, never crashes): read from `side`
  **or** `side_classification`, upper-cased, looked up in the frozen
  `_UW_SIDE_TO_FILL_SIDE` table. An unrecognised value → `fill_side =
  "unknown"` (the already-pinned behaviour for unknown `side`).

  Honest limit: on the recent-flow endpoint `side_classification` is a
  *directional* label (`bullish`/`bearish`/`neutral`, per
  `sector_peer.py`), not a *fill* label (`ask`/`bid`). Those directional
  values are **not** in the fill-side table, so they map to
  `fill_side="unknown"`. That is deliberate — we do not invent a fill side
  from a direction. Accepting both **names** covers a genuine field rename;
  it does not (and must not) fabricate fill semantics from a directional
  field. Downstream direction is derived from `option_type`, not from this
  slot, so `fill_side="unknown"` is safe.

## 5. What is NOT verifiable here

No live UW credential is available in the build environment, so the exact
JSON field names the recent-flow endpoint returns for `strike`, `premium`,
`price`, `bid`, `ask`, `option_type`, `expiry` cannot be confirmed against
the live API. The design makes any mismatch **loud**: the mapper logs the
row's real keys at ERROR and skips the row, rather than emitting a
fabricated or silently-dropped print. Berkay's first live run against real
UW will either produce prints or produce a single ERROR line naming the
exact field to rename — a one-line fix, not a debugging session.

## 6. Done when

- `map_uw_flow_event` is a module-level pure function used by both sources;
  the WS suite (`tests/unit/test_unusual_whales_live.py`) passes unchanged.
- The mapper handles REST-shaped rows (`side_classification`) and emits the
  keys-listing ERROR on a missing-required-field row.
- `UnusualWhalesRestFlowSource` yields correct `RawPrint`s from a fake
  injected client returning `{"data": [...]}`, with no live network.
- `screener --source rest --live-tickers …` runs the full pipeline + digest
  (verified with a fake client in an integration test).
- Every commit green: `uv run pytest -q && uv run mypy --strict src/ &&
  uv run ruff check .`. No profile threshold changed. No `# TODO`.

## 7. Non-goals

- No streaming / reconnect for REST (one-shot by design).
- No new scoring, no new thresholds, no profile edits.
- No change to `run`, to the WS source's behaviour, or to ThetaData.
- Not a claim of edge. Phase 3.5 owns the edge verdict.
