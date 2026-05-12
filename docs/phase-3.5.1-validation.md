# Phase 3.5.1 — Local credential validation

**Status: KAPALI.** Phase 3.5 contract condition satisfied:
local integration smoke tests pass against real ThetaData and
Unusual Whales endpoints with operator credentials loaded.

## Run details

- Date: **2026-05-12**
- Operator: Berkay
- Machine: local (darwin 25.4.0)
- ThetaData Terminal: v3, port 25503, OPTION.STANDARD + STOCK.FREE
- UW subscription tier: API-Plus (key valid for all six providers
  + the new `/api/stock/{ticker}/ohlc/{candle_size}` endpoint
  consumed by M23 since Phase 3.3.8.3)
- Branch HEAD at validation: Phase 3.3.9.7 closeout commit

## Smoke results

Command:
```bash
cd /Users/berkay/Documents/uoa-detector-
set -a; . ./.env; set +a
PYTHONPATH=src .venv/bin/python -m pytest tests/integration/ -q
```

Result:
```
81 passed, 1 skipped in 18.40s
```

The single skip is `test_live_observer_smoke.py::*` which
correctly skips outside US RTH (09:30-16:00 ET) — confirmed by
the recorded skip reason. There were **no failures**.

## Breakdown

| Group | Count | Status |
|---|---:|---|
| UW provider smokes (`test_unusual_whales_smoke.py`) | 8 | ✅ all pass |
| ThetaData provider smokes (`test_thetadata_smoke.py`) | 3 | ✅ all pass |
| M-module integration (M21-M28 individual smokes) | 8 | ✅ all pass |
| M23 UW spot-OHLC smoke (Phase 3.3.8.3) | 1 | ✅ pass |
| CLI / profile / e2e / hot-swap | 60 | ✅ all pass |
| `test_live_observer_smoke` (live_market) | 1 | ⏭ skipped (off-hours; expected) |
| **Total** | **82** | **81 pass + 1 expected skip** |

## Issues encountered during Phase 3.5.1 (resolved before closeout)

1. **UW endpoint path drift** — 12 of the originally-listed smoke
   targets returned HTTP 404 on the first run because Phase 3.3.3
   (Q4 2024) provider URL strings no longer matched UW's current
   REST surface. UW response bodies included a targeted LLM/agent
   advisory pointing at the documented fix procedure.

   Resolution: Phase 3.3.9 (UW endpoint path migration) shipped
   in 7 sub-commits as part of this Phase 3.5.1 effort. Details
   in `docs/phase-3.3.9-acceptance.md`.

2. **M23 ThetaData STOCK.VALUE subscription gap** — surfaced in
   Phase 3.3.7.5 smoke validation prior to Phase 3.5.1's first
   attempt. Resolved in Phase 3.3.8 by switching M23's spot-OHLC
   backend from ThetaData to UW. Details in
   `docs/phase-3.3.8-acceptance.md`.

3. **OI smoke fixture used a 2024 contract** — UW's
   `historic_data_access` window is the trailing ~7 trading
   days; the smoke ran fine against the new `/historic`
   endpoint but the 2024-11-15 trade_date param was
   subscription-rejected. Resolution: smoke fixture refreshed
   in Phase 3.3.9.7 to use `today + 28d` expiry and
   `today - 2d` trade_date — both inside the operator's
   window.

4. **`test_live_source_missing_uw_key_rejected` polluted by
   loaded `.env`** — `BaseSettings._env_file` auto-loaded
   `.env` even after `monkeypatch.delenv`. Resolution: test now
   patches `Credentials.model_config._env_file` to a
   non-existent path so the BaseSettings init reads only env
   vars (which are cleared).

## Pre-existing baseline unit-test failures (NOT regressions)

Three unit tests fail on the operator's local machine because
they depend on credentials being absent from the environment.
With the operator's `.env` loaded and shell-exported keys, they
incorrectly see credentials as present. These are NOT new
failures and would pass in fresh CI:

  - `tests/unit/test_calibration_yaml_loading.py::test_warning_logged_for_commonly_tuned_missing`
    (structlog → caplog bridge fragility)
  - `tests/unit/test_live_factory.py::test_missing_uw_key_fails_fast`
  - `tests/unit/test_live_factory.py::test_missing_thetadata_key_fails_fast`

Phase 3.3.9 did not touch these; cleanup deferred (low priority,
not blocking any phase).

## Operator confirmations

  - Theta Terminal v3 was started prior to the run and remained
    live (port 25503 reachable, `/v3/option/list/symbols`
    returns 200).
  - `.env` is present at repo root, gitignored, contains
    `UNUSUAL_WHALES_API_KEY`, `THETADATA_API_KEY`,
    `THETADATA_USERNAME` — none committed.
  - Berkay confirmed Theta Terminal can be re-launched on
    demand for Phase 3.5.3's hours-to-days historical
    download.

## Sıradaki

Phase 3.5.1 KAPALI → Phase 3.5.2 (historical download dry-run on
5 ticker-months) starts in the same agent run, then Phase 3.5.4
(synthetic 4-cell backtest pre-flight). Phase 3.5.3 (real
Tier-2 download, hours-to-days wall clock) remains Berkay's
responsibility — agent does not run that command.
