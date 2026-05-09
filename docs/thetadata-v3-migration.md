# ThetaData v2 → v3 migration mapping

This is the working spec for Phase 3.3.7. Built by surveying the
existing v2 adapter (`src/uoa_detector/sources/thetadata/`) and
reading ThetaData's official v3 docs at
`docs.thetadata.us`. Authoritative for the migration; if a sub-phase
discovers a discrepancy with reality (per Phase 3.3.7 risk section),
that discovery is logged here as an addendum, not a re-opening of
this doc.

---

## 1. Adapter surface today (v2)

Phase 3.3.2 ships four files under
`src/uoa_detector/sources/thetadata/`:

| File | LoC | Role |
|------|----:|------|
| `mapping.py` | 455 | TradeRow / QuoteRow Pydantic models; positional-array → typed mapping; OPRA condition + exchange code lookup; ET→UTC datetime; v2 strike encoding (1/10 cent integer) |
| `client.py` | 309 | HTTP client with retry/timeout/circuit-breaker; `request_json` + `stream_ws` |
| `historical.py` | 506 | Per-contract historical fetcher; uses trade + quote endpoints |
| `live.py` | 471 | WebSocket live source; subscribe by contract; reconnect with backoff |

### v2 endpoints we currently call

```
GET  /v2/list/roots/option           — smoke test only
GET  /v2/hist/option/trade           — historical trades per contract
GET  /v2/hist/option/quote           — historical quotes per contract
WS   ws://127.0.0.1:25520/v2/ws      — live trades/quotes stream
```

### v2 parameter names we currently use

| v2 name | v2 type / encoding | Where used |
|---------|---------------------|------------|
| `root` | `str` (uppercase ticker) | historical, live subscribe |
| `exp` | `str` (`YYYYMMDD`) | historical, live subscribe |
| `strike` | `int` (1/10 cent — $170 → `170000`) | historical, live subscribe |
| `right` | `'C'` / `'P'` | historical, live subscribe |
| `start_date` / `end_date` | `str` (`YYYYMMDD`) | historical |

### v2 response shape we currently parse

```json
{
  "header": {
    "format": ["ms_of_day", "sequence", "ext_condition1", ...,
               "condition", "size", "exchange", "price",
               "condition_flags", "price_flags", "volume_type",
               "records_back", "date"]
  },
  "response": [
    [43860664, 602567584, 0, 0, 0, 0, 0, 5, 7, 18.50, 0, 0, 0, 0, 20240116],
    ...
  ]
}
```

The response is positional. `trade_row_from_array(values, fields)`
zips them into a TradeRow Pydantic model. All timestamps come as
`(date_yyyymmdd, ms_of_day)` integer pairs and are converted to UTC
via `et_ms_to_utc_datetime()`.

### v2 default ports

  - REST: 25510
  - WebSocket: 25520

---

## 2. v3 endpoint mappings (per official migration guide)

### REST endpoints

| v2 endpoint | v3 endpoint | Used by us? |
|-------------|-------------|:-----------:|
| `/v2/list/roots/option` | `/v3/option/list/symbols` | ✓ (smoke) |
| `/v2/hist/option/trade` | `/v3/option/history/trade` | ✓ (historical) |
| `/v2/hist/option/quote` | `/v3/option/history/quote` | ✓ (historical) |
| `/v2/hist/option/eod` | `/v3/option/history/eod` | — |
| `/v2/hist/option/{req}` (bulk via missing strike+right) | `/v3/option/history/{req}` (bulk via `*`) | — |
| `/v2/snapshot/option/{req}` | `/v3/option/snapshot/{req}` | — |
| `/v2/at_time/option/{req}` | `/v3/option/at_time/{req}` | — |

The two endpoints we actively use are `option/history/trade` and
`option/history/quote`. Smoke uses `option/list/symbols`.

### Streaming (WebSocket) endpoint

The migration guide does not cover the WebSocket. Per
`docs.thetadata.us/Streaming/Getting-Started.html`:

  - **Old**: `ws://127.0.0.1:25520/v2/ws`
  - **New**: `ws://127.0.0.1:25520/v1/events`

Note: streaming is `v1` (not `v3`) — it has its own versioning
independent of the REST API. **Port 25520 unchanged.**

Streaming message format also unchanged from what live.py expects:
the example contract object on the v3 streaming docs page still
uses `root`, `expiration` (YYYYMMDD int), `strike` in 1/10ths of
a cent (140000 = $140), and `right` `'C'`/`'P'`. live.py's
mapping is correct as-is for the message format; only the URL path
needs updating.

### Default ports

  - REST: **25510 → 25503** (per acceptance contract decision #4
    and confirmed in v3 docs sample URLs)
  - WebSocket: **25520 unchanged**

---

## 3. v3 parameter mappings (REST only)

| v2 param | v3 param | Type/encoding change |
|----------|----------|----------------------|
| `root` | `symbol` | str (no change in value, only name) |
| `exp` | `expiration` | accepts `YYYYMMDD`, `YYYY-MM-DD`, or `*` |
| `strike` | `strike` | **Was `int` 1/10 cent; now `str` dollars** (`"170.00"` for $170; supports `*`) |
| `right` | `right` | **Was `'C'`/`'P'`; now `'call'`/`'put'`/`'both'`** (default `both`) |
| `start_date` / `end_date` | `start_date` / `end_date` | Unchanged for OPTION history; REMOVED for stock history (returns one day). We use options, so no impact. |
| `ivl` (interval ms int) | `interval` (str like `1m`/`5m`/`1h`) | Not currently used |
| `rth` (regular trading hours bool) | — | Replace with `start_time`/`end_time`. We currently don't pass `rth`; we filter via `et_ms_in_regular_hours()` in mapping. **No change needed here.** |
| `use_csv` | `format` | `csv` / `json` / `ndjson` / `html`. **Default is `csv`** — we MUST set `format=json` explicitly. |
| `pretty_time` | — | v3 uses pretty timestamps by default. Not currently used. |

### Most impactful changes for our code

1. **Strike encoding**: v2 historical params used `170000` (int 1/10
   cent); v3 expects `"170.00"` (string in dollars). The internal
   `Decimal` representation in `OptionsContract` doesn't change —
   only the URL serialization function does.

2. **Right values**: `'C'`/`'P'` → `'call'`/`'put'`. Both URL params
   AND response shape carry this — affects mapping module too.

3. **Default response format is CSV**: We MUST add `format=json` to
   every request, otherwise we'll get unparseable CSV bytes.

---

## 4. v3 response shape (THE big change)

This is NOT in the migration guide directly; surfaced from
`docs.thetadata.us/operations/option_history_trade.html`.

### v2 response (positional arrays)

```json
{
  "header": {"format": ["ms_of_day", "sequence", ..., "date"]},
  "response": [[43860664, 602567584, ..., 20240116], ...]
}
```

### v3 response (array of named objects)

```json
[
  {
    "symbol": "AAPL",
    "expiration": "2024-11-08",
    "strike": 220.00,
    "right": "call",
    "timestamp": "2024-11-04T09:30:00.000",
    "sequence": 12345,
    "ext_condition1": 0,
    "ext_condition2": 0,
    "ext_condition3": 0,
    "ext_condition4": 0,
    "condition": 0,
    "size": 1,
    "exchange": 7,
    "price": 12.50
  },
  ...
]
```

### Concrete differences from v2

| Aspect | v2 | v3 |
|--------|----|----|
| Top-level shape | `{"header": ..., "response": [[...]]}` | `[{...}, {...}]` (no header wrapper) |
| Field encoding | Positional array indexed by `header.format` | Named object per row |
| Timestamp | `(date: int YYYYMMDD, ms_of_day: int)` integer pair | `timestamp: str` ISO `YYYY-MM-DDTHH:mm:ss.SSS` |
| Strike (response) | (not in response, per-request) | `strike: float` in dollars |
| Right (response) | (not in response, per-request) | `right: str` `"call"` / `"put"` |
| Trade fields | 15 positional fields (4 ext_condition + condition + size + exchange + price + various flags) | Same fields, but as named dict; flags fields appear OMITTED in v3 (not in trade endpoint response schema) |

### Quote response

Not fully documented at the URL level here, but per-pattern:

  - v2: `[ms_of_day, bid_size, bid_exchange, bid, bid_condition,
    ask_size, ask_exchange, ask, ask_condition, date]` positional
  - v3: `{timestamp, bid_size, bid_exchange, bid, bid_condition,
    ask_size, ask_exchange, ask, ask_condition}` (named, with ISO
    timestamp)

This will be confirmed in Phase 3.3.7.2 implementation against a
sample v3 response.

---

## 5. Identified breaking changes that affect our adapter

Ordered by impact:

**B1. REST response is now array-of-objects (no `{header, response}`
wrapper).** Touches every JSON parsing path. `client.request_json`
returns a `dict`; we'll need an `array_or_dict` return type or
specialise the response parsers.

**B2. Timestamps now ISO strings, not (date, ms_of_day) pairs.**
`et_ms_to_utc_datetime()` is partially obsolete — it's still useful
for the streaming WS messages (which use ms_of_day), but the REST
mapping path needs ISO parsing. Both representations end up as the
same UTC datetime; the conversion code branches.

**B3. Strike encoding changes in REST URL params** (1/10 cent int →
dollars str). `thetadata_dollars_to_strike()` (currently returns
`int`) needs a sibling `thetadata_dollars_to_strike_v3_param()`
that returns `str` like `"170.00"`. Or the existing function gets
re-purposed; v2 caller is going away.

**B4. Right encoding changes in REST URL params** (`'C'` → `'call'`).
Trivial mapping but touches every URL builder.

**B5. Default response format is CSV.** Every request must add
`format=json` query param. One-line fix in `request_json`.

**B6. WebSocket URL path change** (`/v2/ws` → `/v1/events`). One-
line constant change in `live.py`. Streaming message shape
unchanged, so the parsing code below the URL is unaffected.

**B7. Default REST port changes** (25510 → 25503). One-line constant
change in `client.py` + profile YAML.

---

## 6. Items deferred — v2 features we don't use, v3 changes we ignore

  - `bulk_hist`-style endpoints — we built per-contract iteration in
    `historical.py` rather than calling bulk endpoints, so the
    consolidation `hist + bulk_hist → history (with * wildcard)`
    affects us in description only. Per-contract URL pattern still
    works.
  - Greeks endpoints (new in v3) — out of scope; M-modules don't
    use Greeks at this phase.
  - IV snapshot endpoint (new in v3) — UW's IV history is the M24
    source; ThetaData IV is unused.
  - MCP server (new in v3 beta) — not used.
  - At-time endpoints — not used by current modules.
  - Snapshot endpoints — not used by current modules.
  - Index data endpoints — out of scope.
  - Calendar endpoints — out of scope (Phase 3.5 trading-calendar
    overlay deferred).
  - `option/history/eod` — used by some bulk download patterns; our
    download path uses minute-granular trade fetches, not EOD.

---

## 7. Items needing judgment (TBD — to be resolved during 3.3.7.2-4)

### J1. Should `et_ms_to_utc_datetime` survive?

After 3.3.7, REST callers use ISO timestamp parsing. Streaming WS
callers still use `(date, ms_of_day)`. Decision: **keep
`et_ms_to_utc_datetime` for streaming**, add new
`iso_timestamp_to_utc_datetime` for REST. Both exported. Tests
cover both. Resolved in 3.3.7.2.

### J2. Trade response field shape: how many extra fields will v3 add?

The v3 trade docs page lists 13 fields per row (symbol, expiration,
strike, right, timestamp, sequence, ext_condition1-4, condition,
size, exchange, price). v2 had 15 (the missing ones are
`condition_flags`, `price_flags`, `volume_type`, `records_back`).
Decision: **TradeRow Pydantic model uses `extra="ignore"` (was
`extra="forbid"`) for forward-compat against v3 adding fields**.
Resolved in 3.3.7.2.

### J3. Quote response: v3 doesn't document field-by-field

The v3 quote endpoint docs page wasn't fetched in 3.3.7.1; we'll
verify against a real Theta Terminal v3 response in 3.3.7.5.
**Working assumption**: same fields as v2 (bid_size, bid_exchange,
bid, bid_condition, ask_size, ask_exchange, ask, ask_condition,
timestamp), but as named dict with ISO timestamp. If reality
differs, the sample v3 fixture in 3.3.7.2 will fail and we adjust.
Resolved in 3.3.7.5 if not earlier.

### J4. Strike string format precision

v3 example URL: `strike=220.000` (3 decimals); doc says
"in dollars (ie `100.00` for `$100.00`)" (2 decimals). Both seem
to work. Decision: **emit 2 decimals from `Decimal.quantize`** to
match the doc's canonical example. v3 endpoint clearly tolerates
either; 2 decimals is the human-readable convention for USD.
Resolved in 3.3.7.3.

### J5. `format=json` always-on, or per-call?

We always want JSON. Decision: **`request_json` ALWAYS injects
`format=json` into params**, callers don't have to think about it.
Resolved in 3.3.7.3.

### J6. Profile field name for the new port

`data_sources.thetadata.host` is currently the full
`http://127.0.0.1:25510` URL. Decision: **rename to
`base_url` would be ideal but affects every config consumer; keep
`host` as the field name, update default value**. The acceptance
contract item #4 says "default port assumption changes from 25510
to 25503" — that's a value change, not a field rename. Resolved
in 3.3.7.3.

### J7. `start_date`/`end_date` STILL valid for option history

The v3 trade endpoint docs explicitly accept both `start_date`/
`end_date` AND `date` (single date). The migration guide footnote
about "no longer includes start_date/end_date" applied to STOCK,
not option. Decision: **historical.py keeps start_date+end_date
for date-range requests**; no change to the per-contract date
window logic. Resolved in 3.3.7.3.

---

## 8. Migration sequence (concrete sub-phase contents)

This restates the acceptance contract sub-phases in code-target
terms.

### 3.3.7.2 — `mapping.py`

Files touched:
  - `src/uoa_detector/sources/thetadata/mapping.py`
  - `tests/unit/test_thetadata_mapping.py`

Changes:
  - Add `iso_timestamp_to_utc_datetime(s: str) -> datetime` for
    REST parsing
  - Keep `et_ms_to_utc_datetime` for streaming parsing (J1)
  - Add `parse_v3_right(s: str) -> Literal["C", "P"]` to internal
    canonical (we keep "C"/"P" internally; v3 input is decoded at
    boundary)
  - Add `format_v3_right(opt_type: str) -> Literal["call", "put"]`
    for URL params
  - Add `format_v3_strike_param(dollars: Decimal) -> str` returning
    e.g. `"170.00"` (J4)
  - Keep `thetadata_dollars_to_strike` and
    `thetadata_strike_to_dollars` for streaming use (1/10 cent
    encoding still in WS messages)
  - TradeRow / QuoteRow get `extra="ignore"` (was forbid, J2)
  - Add `trade_row_from_v3_dict(obj: dict) -> TradeRow` parser
  - Add `quote_row_from_v3_dict(obj: dict) -> QuoteRow` parser
  - Existing `trade_row_from_array` + `quote_row_from_array`
    retained for streaming-message parsing where positional arrays
    still appear

Tests:
  - All existing mapping tests stay green (streaming path unchanged)
  - 8-12 new tests for v3 parsers + v3 right/strike formatters

### 3.3.7.3 — `client.py` + `historical.py`

Files touched:
  - `src/uoa_detector/sources/thetadata/client.py`
  - `src/uoa_detector/sources/thetadata/historical.py`
  - `tests/unit/test_thetadata_client.py`
  - `tests/unit/test_thetadata_historical.py`

Changes:
  - `DEFAULT_BASE_URL = "http://127.0.0.1:25503"` (was 25510)
  - `request_json` injects `format=json` if not present (J5)
  - `request_json` parses array-or-dict response (the smoke
    `option/list/symbols` may still return a dict with a list
    inside — verify in 3.3.7.5; for trade/quote arrays go through
    `trade_row_from_v3_dict` etc.)
  - `historical.py` URL builders:
    - `/v2/hist/option/trade` → `/v3/option/history/trade`
    - `/v2/hist/option/quote` → `/v3/option/history/quote`
    - `root` → `symbol`
    - `exp` → `expiration`
    - `strike` value uses `format_v3_strike_param(strike_dollars)`
    - `right` uses `format_v3_right(option_type)` mapping
      `"call"` → `"call"`, `"put"` → `"put"`
  - Per-day date params unchanged (J7)
  - Retry / timeout / circuit-breaker / rate-limit logic untouched

Tests:
  - `test_thetadata_client.py` URL fixtures updated to v3
  - `test_thetadata_historical.py` mocked HTTP transport returns
    v3-shape arrays; parser asserts work against new mapping calls

### 3.3.7.4 — `live.py`

Files touched:
  - `src/uoa_detector/sources/thetadata/live.py`
  - `tests/unit/test_thetadata_live.py`

Changes:
  - WS URL: `/v2/ws` → `/v1/events`
  - Port 25520 unchanged
  - Subscription payload format: per Streaming docs, contracts
    sent in subscribe still use `root` + `expiration` (YYYYMMDD
    int) + `strike` (1/10 cent int) + `right` (`C`/`P`). Our
    current code does this exactly. **No payload format change.**
  - Stream message parser unchanged (uses
    `et_ms_to_utc_datetime` still)

Tests:
  - URL constants updated in fixtures
  - All existing tests stay green (parsing logic unchanged)
  - Reconnect tests untouched (no semantic change)

### 3.3.7.5 — Smoke + closeout

Files touched:
  - `tests/integration/test_thetadata_smoke.py`
  - `docs/phase-3.3.7-validation.md` (new — Berkay-filled template)
  - `docs/phase-3.3-acceptance.md` (closeout summary appended)

Changes:
  - Smoke endpoint paths updated to v3
  - Default URL `http://127.0.0.1:25503`
  - All 3 ThetaData smokes still gated by `THETADATA_API_KEY`
  - Berkay runs against real Terminal v3, captures pass/fail in
    validation doc
  - M23 smoke (Phase 3.4.3) re-runs to confirm provider injection
    seam holds across the v3 backend swap

---

## 9. Profile + config YAML changes

`profiles/v5_default.yaml` (and inheritors) currently have:

```yaml
data_sources:
  thetadata:
    host: "http://127.0.0.1:25510"
    # ... rate limits, timeouts, etc
```

After 3.3.7.3:

```yaml
data_sources:
  thetadata:
    host: "http://127.0.0.1:25503"
    # ... rate limits, timeouts, etc unchanged
```

That's the only profile change. v5_gamma_squeeze inherits.

---

## 10. Backward-compat invariants to preserve

These do NOT change in Phase 3.3.7:

  - **Parquet schema** for downloaded historical data
    (`parquet_schema.RAWPRINT_PARQUET_SCHEMA`). The file format on
    disk stays the same; only the in-flight wire format changes.
    Phase 3.5.3 download produces identical parquet output.
  - **`OPRA_DROP_CONDITIONS` set** in mapping.py — condition codes
    are an OPRA standard, not ThetaData-specific.
  - **`_EXCHANGE_NAMES` dict** — exchange codes are OPRA standard.
  - **Internal canonical types** (`OptionsContract`, `RawPrint`)
    from `domain/`. The boundary mapping changes; the canonical
    types do not.
  - **M23 stage** (`PriceConfirmationStage`). Provider injection
    seam is `ThetaDataPriceActionProvider`; only that provider's
    internal HTTP shape changes.
  - **Phase 3.3.4 download script** (`scripts/download_tier2.py`).
    Calls into historical.py; our refactor preserves
    `historical.py`'s public surface (`fetch_trades`,
    `fetch_quotes`).
  - **Auth / rate-limit / retry / circuit-breaker** logic in
    `_http_base.py` (Phase 3.3.2's middleware). Independent of
    v2/v3 endpoint paths.

---

## 11. Validation strategy after 3.3.7

Three layers, each must pass before Phase 3.3.7 closes:

1. **Unit tests** — every existing test stays green; new tests
   added for v3-specific parsers and URL builders. Pinned at
   3.3.7.2-4 commits.

2. **3 ThetaData smoke tests** — gated by `THETADATA_API_KEY`,
   exercises real Terminal v3. Berkay runs locally in 3.3.7.5.

3. **M23 smoke** — exercises the provider-injection seam end-to-end.
   Phase 3.4.3's `ThetaDataPriceActionProvider` is the consumer;
   if the seam holds, M23 keeps passing without any M-stage code
   change.

If ALL THREE pass, Phase 3.3.7 is complete and Phase 3.5.1 resumes
with v3 Terminal already running.

---

## 12. Behaviour-change risks not covered by the migration guide

Per the acceptance contract risk section, these are flagged for
discovery during 3.3.7.2-4 (and as Phase 3.5 bug-fix sub-commits
if they surface later):

  - **CSV-by-default response format**: easy to forget; we add
    `format=json` to request_json; if anyone uses lower-level
    HTTP they may see CSV.
  - **HTTP error code parity**: v3 may return different status
    codes / error message bodies for the same failure modes. Our
    retry / circuit-breaker matches on status codes, not bodies,
    so this is low-risk; surfaces only if v3 introduces a new
    error code we don't whitelist.
  - **Timestamp timezone**: v3 docs say timestamp is
    `YYYY-MM-DDTHH:mm:ss.SSS` — no timezone marker. ThetaData
    historically reports ET; assumption is v3 still does. We will
    parse as ET-naive and convert to UTC in
    `iso_timestamp_to_utc_datetime`.
  - **`format=json` and the `format=csv` default in error responses**:
    if Theta Terminal returns CSV-formatted error bodies when
    JSON was requested, our response parser will fail in a
    confusing way. We flag this for fixture testing in 3.3.7.3.
  - **WebSocket frame format edge cases**: per the streaming docs,
    status messages every second, contract objects per QUOTE/TRADE.
    Our existing parser handles these per Phase 3.3.2; no change
    expected. If v3 streaming silently changed the message shape,
    Phase 3.3.7.4 testing or 3.3.7.5 smoke catches it.

---

## 13. Acknowledgements

Source documents read:

  - `https://docs.thetadata.us/Articles/Getting-Started/v2-migration-guide.html`
    (Section 4 endpoint mapping table; Section 5 parameter mapping
    table)
  - `https://docs.thetadata.us/operations/option_history_trade.html`
    (v3 trade endpoint sample URL; full query params; response
    schema)
  - `https://docs.thetadata.us/Streaming/Getting-Started.html`
    (`ws://127.0.0.1:25520/v1/events` URL; contract object
    structure unchanged)

Survey of current adapter:
  - `src/uoa_detector/sources/thetadata/{mapping,client,historical,live}.py`
  - `tests/unit/test_thetadata_*.py`
  - `tests/integration/test_thetadata_smoke.py`
