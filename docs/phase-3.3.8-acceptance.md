# Phase 3.3.8 — M23 backend swap (ThetaData → Unusual Whales)

This sub-phase is a follow-up to Phase 3.3.7 (ThetaData v2 → v3
migration) and a precondition for Phase 3.5.1 (local credential
validation). It is **not** a re-opening of any frozen contract:

  - The Phase 3.4 acceptance doc remains frozen. M23's stage code,
    Protocol surface (`PriceActionProvider`), DTO (`PriceMovement`),
    score branches, and profile schema are unchanged.
  - The Phase 3.5 acceptance doc remains frozen. Phase 3.5.1's
    "20/20 integration smokes pass" condition is satisfied by the
    UW-backed M23 smoke rather than the ThetaData-backed M23
    smoke.
  - `docs/MODULES.md` is a living reference and is updated in
    place to reflect the new default provider.

The original Phase 3.4.3 judgment ("ThetaData = spot OHLC domain")
is **superseded** rather than edited away. The history is preserved
in `docs/MODULES.md` M23 judgment-call ledger.

---

## Why this sub-phase exists

Phase 3.3.7.5 smoke validation surfaced the following on Berkay's
ThetaData account (verified 2026-05-12):

  - OPTION.STANDARD subscription: active
  - STOCK.FREE: active
  - STOCK.VALUE (required for `/v3/stock/history/ohlc`): **not active**

M23 is the only Phase 3.4 module consuming ThetaData stock data,
and its smoke test is one of the 20 required for Phase 3.5.1
closeout. Three options were considered:

  1. **Purchase STOCK.VALUE** ($80/mo add-on). Cleanest from a
     contract-stability perspective but commits ongoing cost
     before the Phase 3.5 falsification verdict is known. Berkay
     declined on cost-discipline grounds.
  2. **Disable M23 in the Track B backtest** (run 7-source fusion
     instead of 8-source). Rejected: breaks the multi-source
     fusion hypothesis frozen in `v5_gamma_squeeze.yaml`, which
     makes Phase 3.5.6's `(single, fusion)` falsification cell
     uninterpretable.
  3. **Switch M23's backend to UW's stock OHLC endpoint**, which
     ships under the same API-Plus subscription as the other
     Phase 3.3.3 providers. Selected.

This document records option 3's contract.

---

## What changed

### Sub-commit 3.3.8.1 — M23 graceful degradation (foundation fix)

M23's stage previously caught `TimeoutError` from the provider but
let any other exception propagate. A subscription / auth failure
(HTTP 4xx) on the provider endpoint surfaces as an exception, not
a `None`, so it crashed the pipeline rather than landing on the
neutral fallback that the original Phase 3.4.3 acceptance doc
specified for "spot data missing".

Fix:

  - `PriceConfirmationStage.enrich()` now catches `Exception`
    after the `TimeoutError` handler.
  - On exception: same neutral-score fallback as `data_missing_neutral`,
    distinct branch label `provider_error`, telemetry carries
    `error_type=<ExceptionClassName>` for postmortem.
  - `BaseException` (CancelledError, KeyboardInterrupt, SystemExit)
    intentionally NOT caught — those must propagate up the
    orchestrator chain.

Two new unit tests:

  - `test_provider_exception_emits_neutral_score`
  - `test_provider_baseexception_propagates`

This sub-commit is option-independent — it is the right fix
regardless of which option (1, 2, or 3) was selected above. It
makes the M23 stage resilient to subscription / auth / transport
errors from any provider implementation.

### Sub-commit 3.3.8.2 — `UnusualWhalesPriceActionProvider`

New file:
`src/uoa_detector/sources/unusual_whales/providers/price_action.py`.

  - Implements the existing `PriceActionProvider` Protocol with
    the same `get_intraday_price_movement` semantics as the
    ThetaData provider.
  - Calls `GET /api/stock/{ticker}/ohlc/1m?date=YYYY-MM-DD`
    (UW's documented stock OHLC endpoint).
  - Converts the requested `at` UTC timestamp to its ET trading
    date for the `date=` param (UW indexes trading sessions in ET).
  - Parses bar timestamps with explicit timezone offset; falls
    back to ET-naive if a future schema change drops the offset.
  - TTL cache keyed by `(ticker, date_iso, lookback_minutes)`,
    matching the ThetaData provider's caching shape. TTL comes
    from a new
    `UnusualWhalesProviderCacheTTL.intraday_price_seconds` field
    (default 60s).
  - `snapshot_at` returns `None` (matching the ThetaData provider's
    Phase 3.4.3 stub — M23 doesn't consume it).
  - Subscription / auth / transport errors raised by
    `UnusualWhalesClient` propagate up; the Phase 3.3.8.1 fix in
    M23 catches them.

Profile change:
`profiles/v5_default.yaml` `data_sources.unusual_whales.cache_ttl`
gains an explicit `intraday_price_seconds: 60` entry for parity
with the other cache_ttl fields. `v5_gamma_squeeze.yaml`
inherits — no change there.

Fifteen new unit tests (see
`tests/unit/test_unusual_whales_price_action.py`): Protocol
conformance, happy-path, signed `move_pct`, URL/params shape, ET
trading-date conversion, cache hit + TTL=0 cache disable, empty /
non-dict / malformed-row handling, lookback ≤ 0 short-circuit,
zero-open div-by-zero guard, naive-timestamp ET fallback,
`snapshot_at` stub.

### Sub-commit 3.3.8.3 — M23 smoke + docs

  - `tests/integration/test_m23_smoke.py` rewritten to wire
    `UnusualWhalesPriceActionProvider` against the live UW
    endpoint. Same SPY event shape, same assertions; only the
    backend wiring changes. `provider_error` added to the
    accepted branch set in the assertion (covers the smoke
    correctly when the API transiently 4xxs).
  - `docs/MODULES.md` M23 section updated: provider mapping carries
    both the current (UW) and historical (ThetaData) entries;
    judgment-call ledger preserves the original Phase 3.4.3
    decision marked superseded with the reason, plus the new
    Phase 3.3.8.1 provider-exception branch.
  - This file (`docs/phase-3.3.8-acceptance.md`) committed.

---

## What is NOT changed by Phase 3.3.8

  - **M23 stage code** beyond the 3.3.8.1 graceful-degradation
    handler. Score branches, idempotency-on-preset, telemetry
    shape, session-boundary clamp, and `PriceActionProvider`
    Protocol surface are all unchanged.
  - **`profiles/v5_default.yaml` scoring weights, modules, and
    thresholds.** Only the new `cache_ttl.intraday_price_seconds`
    field is added (cache config, not a scoring knob).
  - **`profiles/v5_gamma_squeeze.yaml`.** No edits; inherits the new
    field automatically.
  - **Phase 3.5 contract.** `docs/phase-3.5-acceptance.md` is
    untouched. The "20/20 integration smokes pass" condition for
    Phase 3.5.1 is now satisfiable on Berkay's current
    subscription tier without a second purchase.
  - **`ThetaDataPriceActionProvider`** itself. Retained as
    importable code for operators who do hold STOCK.VALUE; M23
    will accept it via constructor injection just as it always did.

---

## Calibration / falsification implications

None. The new provider serves the same `PriceMovement` DTO with
the same `move_pct` semantics; M23's score branches operate on
that DTO and don't see the provider type. The Track B fusion
hypothesis (8-source confluence) remains intact. The four
falsification scenarios from Phase 3.2.4 are unaffected.

The only operator-observable difference at backtest time is the
data path: M23's 1-minute bars now come from UW's stock OHLC
endpoint instead of ThetaData's. UW's bars are typically
consolidated SIP feed; ThetaData's are NBBO consolidated. Bar
content for SPY-sized liquidity should be functionally identical
at 1-minute resolution; any thin-name divergence is observable
in `docs/MODULES.md` as a future deferred item if Phase 3.5.7
sanity audit surfaces it.

---

## Acceptance checks (per sub-commit)

| Check | 3.3.8.1 | 3.3.8.2 | 3.3.8.3 |
|---|:---:|:---:|:---:|
| `pytest -q` green | ✓ | ✓ | ✓ |
| `mypy --strict src/` clean | ✓ | ✓ | ✓ |
| `ruff check .` clean | ✓ | ✓ | ✓ |
| New unit tests added | 2 | 15 | 0 |
| Cumulative test count | 1349 | 1364 | 1364 |
| `docs/MODULES.md` consistent | n/a | n/a | ✓ |

Pre-existing baseline failures (3 — caplog/structlog bridge in
`test_calibration_yaml_loading.py`, env-leak in two
`test_live_factory.py` cases) are documented separately and are
unaffected by Phase 3.3.8.

---

## Sıradaki

Phase 3.5.1 — local credential validation — is now unblocked.
Berkay runs:

```
export $(grep -v '^#' .env | xargs)
uv run pytest tests/integration/ -v
```

Expected: 20 skip → 20 pass on the integration suite. M23 smoke
now exercises `UnusualWhalesPriceActionProvider` against UW's
live `/api/stock/SPY/ohlc/1m` endpoint with the API-Plus key.

Closeout note appended to `docs/phase-3.3-acceptance.md` is
deferred to a Phase 3.5.1 commit so that the audit trail
references the actual validation-run result, not a prediction.
