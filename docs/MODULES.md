# Phase 3.4 Modules — Per-Module Reference

This document is the single reference for the eight detection modules
implemented during Phase 3.4: M21 (dealer gamma) through M28 (next-day
OI confirmation). Each module section covers four things:

  1. **What it computes** — the module's role in the v5 spec.
  2. **Provider mapping** — which Phase 3.3 provider it consumes.
  3. **Default thresholds + tuning rationale** — what the operator can
     turn, with reasoning for the default values.
  4. **Judgment-call ledger** — the architectural decisions made during
     implementation that aren't captured by the spec or tests.

This is a living reference, not a contract. Phase 3.4's contract lives
in `docs/phase-3.4-acceptance.md`. When that contract conflicts with
this document, the contract wins; this document is updated to match.

---

## Module taxonomy

Phase 3.4 modules split into two architectural families:

**PipelineStages** (M21-M27) — run at event time, in-process, by the
orchestrator. Each consumes a Phase 3.3 provider via constructor
injection, produces a sub-score field on `EnrichedEvent`, and may
emit a `ScoreAdjustment` for cross-module penalties or bonuses.

**Validators** (M28) — run post-event as scheduled batch jobs,
typically via `scripts/run_m28_overnight.py`. Read completed
`StoredSignal` records, fetch T+1 data, write back via
`store.update_signal_score()`. Different lifecycle, different package
(`pipeline/validators/` not `pipeline/stages/`).

This taxonomy was settled in Phase 3.4.8 when M28 became the first
non-stage module. Phase 3.4.1-3.4.7 modules predate the split but are
all PipelineStages in retrospect.

---

## UW provider lineage

The Unusual Whales providers behind M21–M27 (and M28's OI fetch) went
through three generations. `main` runs only the last one.

| Generation | Lineage | Status |
|---|---|---|
| Phase 3.3.3 adapter paths (M23 moved to UW `ohlc/1m` in 3.3.8) | pre-fork, both lineages | guesses. The provider paths returned HTTP 404 live on 2026-09-14; only M23's path was correct (`docs/phase-3.9-closeout.md` §1–§2). |
| Phase 3.3.9 path migration (`76e1a16`…`1cc04c1`, `docs/phase-3.3.9-acceptance.md`) | `phase-3` only | superseded, not ported. 3.9 did port two later phase-3 pieces: the 4.6 flow-alerts fill-side rule (`d55c148`) and the 4.18 429 retry (`45225f2`). |
| Phase 3.9 endpoint correction (`dd287fa`…`6e44bcd`) | `main`; kept by the Phase 5.0 merge (`6d1e4ca`) | current, live-verified 2026-09-14 |

3.3.9 was known wrong live in four places. The 3.9 contract §2 lists them;
each was checked against the `f0d469f` provider code:
- **M27/M28 open interest.** `open_interest.py` read `chains[0]` from
  `/api/option-contract/{sym}/historic`. That row is always today's. 3.9
  selects the as-of row (§3.6).
- **M22 earnings.** `catalyst_calendar.py` mapped the `report_time` values
  `pre-market` and `after-hours`, and `after-hours` never occurs live. 3.9
  maps `premarket` to 09:30 ET, and `postmarket` or `unknown` to 16:00 ET
  (§3.5).
- **M25 peer flow.** `sector_peer.py` used per-ticker
  `/api/stock/{t}/flow-recent`, which returns the last 50 trades, about
  8 seconds of tape. 3.9 uses
  `/api/option-trades/flow-alerts?ticker_symbol=<peers>` (§3.7).
- **M21 dealer gamma.** `dealer_gamma.py` treated `greek-exposure/strike`
  `call_gex + put_gex` as USD per 1% move. 3.9 reads USD per 1% from
  `/api/stock/{t}/spot-exposures` (`gamma_per_one_percent_move_oi`). It uses
  `greek-exposure/strike` only for the flip strike, where the unit does not
  matter (§3.2).

The 16 phase-3 provider tests that left the spec are listed in the `6d1e4ca`
commit body, each with its replacement. For the current endpoints see
`docs/phase-3.9-uw-endpoint-correction-acceptance.md` §2–§3 and the
"Provider mapping" section of each module below.

---

## M21 — Dealer Gamma Exposure (Phase 3.4.1)

### What it computes

A score in `[0, 1]` reflecting how exposed the dealer book is to
gamma reflexivity around the current spot. High score = dealers are
short-gamma AND spot is near the gamma flip point AND recent
acceleration in net gamma → reflexive hedging cycle is plausible.

The score lives on `EnrichedEvent.gamma_score` and feeds the combined
score via `ScoringWeights.gamma` (default 0.10, Track B 0.25).

### Provider mapping

`UnusualWhalesDealerGammaProvider` (Phase 3.9.5, live-verified 2026-09-14):

  - `aggregate_for_ticker` → `GET /api/stock/{ticker}/spot-exposures?date=<ET date>`.
    `net_gamma_dollars` is `gamma_per_one_percent_move_oi` from the latest
    per-minute row with `time <= event_ts`. It is USD per 1% spot move, the
    unit of `short_gamma_threshold`.
  - `flip_strike` → `GET /api/stock/{ticker}/greek-exposure/strike?date=<ET date>`.
    It is the cumulative zero crossing of `call_gex + put_gex` (share gamma;
    a zero crossing does not depend on the unit).
  - `net_gamma_at` (per strike, no pipeline consumer) → `call_gex + put_gex`,
    in share gamma.

Live note (flagged in the Phase 3.9 closeout): on full UW chains the
unchanged first-crossing `_find_flip_strike` lands on deep-OTM strikes
(AAPL 2026-09-11 → 10 with spot ≈ 332; SPY 2026-09-10 → None), so M21
mostly takes `extreme_distance_cutoff` or loses its proximity leg.

Before Phase 3.9 this section named `/api/option/{ticker}/gex/strikes`,
which never existed. See `docs/phase-3.9-uw-endpoint-correction-acceptance.md` §3.2.

### Default thresholds + tuning rationale

```yaml
m21:
  short_gamma_threshold: -50_000_000     # USD/1% spot move
  flip_proximity_pct: 0.03               # |spot - flip| / spot
  extreme_distance_pct: 0.20             # collapse cutoff
  provider_timeout_s: 2.0
  provider_cache_ttl_s: 300              # 5-min reserve cache
```

`short_gamma_threshold = -$50M/1%` is the institutional-significance
line per market microstructure: below this, dealer hedging flows
move spot enough to matter on a 5-minute horizon. This is a
mechanics number, not a Track B knob.

`flip_proximity_pct = 0.03` (3% of spot) is the band where a small
spot move will cross the flip and trigger reflexive hedging. Track B
tightens to 0.025 (3.4.9.2) for earlier signals.

`extreme_distance_pct = 0.20` collapses the score to 0 when spot is
> 20% away from the flip — at that distance, gamma reflexivity isn't
the relevant mechanic. Track B tightens to 0.15.

### Judgment-call ledger (Phase 3.4.1)

  - **Provider returns three fields, not one** — total / flip_strike /
    nearest_strike — to support per-strike scoring, not just the
    spot-vs-flip distance heuristic.
  - **Score formula uses MAX of the two heuristics** (short-gamma
    activation × flip-proximity, capped by extreme-distance collapse)
    rather than a weighted sum, because the two heuristics overlap
    at the institutional threshold and double-counting was creating
    false positives.
  - **Idempotency-on-preset** via `if event.gamma_score is not None:
    return event` — established the M21-M27 idempotency pattern.
  - **Timeout default 2.0s** — UW dealer-gamma endpoint p95 at ~600ms;
    2.0s is comfortable headroom without blocking the orchestrator
    on transient slowness.

---

## M22 — Event Calendar (Phase 3.4.2)

### What it computes

A score in `[0, 1]` reflecting proximity to a known catalyst event
(earnings / FDA / FOMC / dividend / split). Score peaks 1-3 trading
days before the catalyst, drops to 0 immediately after (the
post-event blackout window), then climbs back toward 0.5 over
subsequent days.

The score lives on `EnrichedEvent.event_score` and feeds the combined
score via `ScoringWeights.event` (default 0.15, Track B 0.05 because
Track B is catalyst-independent by thesis).

### Provider mapping

`UnusualWhalesCatalystCalendarProvider` (Phase 3.9.8, live-verified):

  - earnings → `GET /api/earnings/{ticker}`, which includes the upcoming
    report as `source=estimation`. `report_time` premarket → 09:30 ET;
    postmarket or unknown → 16:00 ET on `report_date`.
  - FDA → `GET /api/market/fda-calendar?ticker=<ticker>`. Only precise dates
    are used (`target_date` as `YYYY-MM-DD`, or `start_date == end_date`).
  - FOMC → `GET /api/market/economic-calendar`. Only `type == "fomc"` rows,
    applied market-wide; the feed covers the current and next week only.
    Live note: on 2026-09-14 the feed typed the 2026-09-16 rate decision as
    `type=report`, so this rule currently yields no FOMC catalysts (flagged).

M22 and M24 share one instance (`live_stages.py`).

### Default thresholds + tuning rationale

```yaml
m22:
  pre_event_window_days: 3               # days before catalyst that score
  post_event_blackout_days: 1            # days after = score 0
  high_score_threshold_days: 1           # within 1 day = peak
  high_score: 1.0
  medium_score: 0.7                      # 2-3 days out
  low_score: 0.3                         # 4-7 days out, ramping back
  no_event_score: 0.5                    # no catalyst in window
  provider_timeout_s: 3.0
  provider_cache_ttl_s: 3600             # calendar updates daily
```

`pre_event_window_days = 3` matches institutional pre-positioning
behaviour (most catalyst-driven flow appears 1-3 days before the
event). `post_event_blackout_days = 1` captures the noise window
where realized vol contaminates flow signals.

`high_score = 1.0` only within `high_score_threshold_days` because
the closer to the event, the lower the noise floor for catalyst-
driven positioning.

### Judgment-call ledger (Phase 3.4.2)

  - **Calendar-day approximation, not trading-day** — the spec said
    "trading days" but Phase 3.4 doesn't have a trading-calendar
    overlay. Using calendar days underestimates pre-window over
    weekends. Flagged for Phase 3.5+: trading-calendar-aware
    overlay.
  - **`no_event_score = 0.5` (not 0.0)** — absence of a known event
    is neutral, not negative. Setting 0.0 would penalize tickers
    with no catalyst calendar coverage (e.g., pre-IPOs or thinly-
    covered names) when they may genuinely have no catalyst.
  - **One provider call per event** — no batching across the 3
    sources (earnings/FDA/FOMC). Provider TTL cache absorbs the
    duplicate-call cost; per-call latency is < 50ms.
  - **No event-type weighting** — earnings vs FDA vs FOMC all score
    the same. The spec separates them only for telemetry; the score
    formula is type-blind. May revisit if backtest shows type-
    specific edge differences.

---

## M23 — Price Confirmation (Phase 3.4.3)

### What it computes

A score in `[0, 1]` reflecting whether the underlying spot moved in
the direction implied by the option flow within a tight window. A
call at $150 strike followed by spot rallying past $150.75 within
5 minutes confirms the bullish thesis; the score reflects the size
of that move scaled by the threshold.

The score lives on `EnrichedEvent.price_confirmation_score` and
feeds the combined score via `ScoringWeights.price_confirmation`
(default 0.10, Track B 0.10 unchanged).

### Provider mapping

**Phase 3.3.8.3 onwards:** `UnusualWhalesPriceActionProvider` → UW
endpoint `/api/stock/{ticker}/ohlc/1m` (1-minute stock OHLC bars).
Returns OHLC for the trading-date containing the event, from which
the provider extracts the open of the earliest in-window bar and
the close of the latest in-window bar.

**Phase 3.4.3 – 3.3.8.2 (deprecated default, still wirable):**
`ThetaDataPriceActionProvider` → ThetaData v3 endpoint
`/v3/stock/history/ohlc?interval=1m`. Retained for operators with a
ThetaData STOCK.VALUE subscription; rest of the system stays
identical because both providers implement the same
`PriceActionProvider` Protocol with the same `PriceMovement` DTO.

**Phase 3.9.11:** `spot_at` is the close of the latest *completed* bar
(`end_time <= event_ts`), and `spot_lookback_ago` is the open of the
earliest completed bar starting at or after the window start. Before
3.9.11 `spot_at` was the close of a bar that started at or before the
event but printed after it (look-ahead). The cache key is (ticker, ET date).

### Default thresholds + tuning rationale

```yaml
m23:
  confirmation_window_minutes: 5         # post-event window
  confirmation_threshold_pct: 0.005      # 0.5% directional move
  full_score: 1.0
  partial_score: 0.5                     # half the threshold
  no_confirmation_score: 0.0
  no_data_score: 0.5                     # provider returned None
  provider_timeout_s: 3.0
  provider_cache_ttl_s: 60               # 1-min OHLC bars
```

`confirmation_threshold_pct = 0.005` (0.5%) captures the magnitude
of move that distinguishes "actual directional flow" from "drift".
Equity options on liquid names see > 0.5% moves regularly within
5 minutes; institutional-driven flow specifically tends to be
followed by precisely this kind of move.

`confirmation_window_minutes = 5` is short enough to attribute the
move to the flow itself (not unrelated catalysts) but long enough
for slow-quote fills to materialize.

### Judgment-call ledger (Phase 3.4.3, amended Phase 3.3.8)

  - **~~ThetaData provider, not UW~~ (superseded Phase 3.3.8.3)** —
    Original 3.4.3 judgment placed spot OHLC under ThetaData's
    domain because that was the natural shape of their endpoints.
    Reality check during Phase 3.3.7 ThetaData v3 smoke validation:
    the stock OHLC endpoint requires a separate STOCK.VALUE
    subscription tier that operators on OPTION.STANDARD don't
    have. Rather than block Phase 3.5.1 on a subscription
    purchase, Phase 3.3.8.3 ships
    `UnusualWhalesPriceActionProvider` against UW's documented
    `/api/stock/{ticker}/ohlc/{candle_size}` endpoint. The
    `PriceActionProvider` Protocol is unchanged; M23 stage code is
    unchanged; only the wiring switches. See
    `docs/phase-3.3.8-acceptance.md`.
  - **Half-day handling deferred to Phase 3.5+** — a 5-minute
    window crossing market close on a half-day (1pm ET) currently
    fetches partial data. The provider returns whatever it has;
    M23 scores against that. Trading-calendar overlay would clip
    the window to [event_ts, market_close).
  - **`no_data_score = 0.5` (not 0.0)** — distinguishes "spot didn't
    move enough" (legitimate score=0) from "we have no spot data"
    (score=neutral). Phase 3.5 backtest cost auditing relies on
    this distinction.
  - **Direction inferred from `option_type`** — call → check high,
    put → check low. The spec implies this but the implementation
    pins it explicitly. Spreads / multi-leg flows aren't covered;
    those are out of M23's scope.
  - **Provider-exception branch (Phase 3.3.8.1)** — the stage
    catches `Exception` from the provider (HTTP 4xx/5xx, auth,
    transport) and maps to `branch=provider_error`, neutral score.
    `BaseException` (CancelledError, KeyboardInterrupt) still
    propagates. Required because subscription-tier mismatches
    surface as HTTP errors, not None responses.

---

## M24 — IV Exhaustion (Phase 3.4.4)

### What it computes

A penalty (NOT a score) applied when implied volatility is at the
top of its 30-day percentile range. Buying options at IV > 80th
percentile is statistically a losing position — the M24 penalty
reduces `combined_score_pre_penalty` by a configurable amount when
the threshold is breached.

The penalty is a `ScoreAdjustment` against `combined_score_pre`,
not a sub-score. M24 doesn't appear in `ScoringWeights`; it acts
on the COMBINED score across all weights.

### Provider mapping

`UnusualWhalesIVHistoryProvider` (Phase 3.9.6) → `GET /api/stock/{ticker}/iv-rank?date=<ET date>`.

  - Ticker-level. `iv_rank_252d = iv_rank_1y`: the live scale is 0–100, so
    there is no rescaling.
  - `implied_volatility = volatility`.
  - Row selection is as-of. A row dated before the event's ET date always
    qualifies. A same-day row qualifies only when `updated_at <= event_ts`,
    because UW publishes the day's row after the close.

### Default thresholds + tuning rationale

```yaml
m24:
  exhaustion_percentile: 0.80            # top 20% of 30-day IV
  penalty_amount: 0.20                   # subtracted from combined_pre
  provider_timeout_s: 3.0
  provider_cache_ttl_s: 1800             # 30-min cache (IV updates slowly)
```

`exhaustion_percentile = 0.80` is the institutional-significance
threshold for "IV is high enough that options buying has poor
expected value". Above 80th percentile, mean-reversion to lower
IV typically dominates directional gains.

`penalty_amount = 0.20` is calibrated to reduce a 0.7 combined
score (mid-WATCH band) to 0.5 (border of consideration), which is
the right strength: not a hard rejection, but a clear demotion.

### Judgment-call ledger (Phase 3.4.4)

  - **Penalty via `ScoreAdjustment`, not sub-score subtraction** —
    cross-module rail. M24 was the FIRST cross-module penalty; its
    `ScoreAdjustment` mechanism became the pattern for any future
    penalties touching `combined_score_pre`.
  - **No M24 score field on EnrichedEvent** — the penalty is
    expressed as an adjustment, not stored on the event. This was
    a clean architectural choice: penalty mechanics shouldn't
    leak into the per-event sub-score schema.
  - **30-day window, not 90-day** — a longer window dilutes the
    signal; 30 days is the standard institutional reference for
    IV percentile.
  - **No score gradient between 80th-90th-95th percentile** —
    binary penalty. A gradient might capture more nuance but adds
    tuning complexity; the binary cut at 80th is defensible without
    backtest data.
  - **Penalty applied to `combined_score_pre`, not `_post`** — the
    adjustment lands BEFORE the labeler reads `combined_score_pre`,
    so high-IV signals get demoted at decision time. `_post` is
    the post-penalty score for telemetry only.

---

## M25 — Sector Peer Confirmation (Phase 3.4.5)

### What it computes

A score in `[0, 1]` reflecting how many tickers in the same sector
saw similar UOA flow in the same window. High score = "this isn't
a one-off; the whole sector is positioning". Low score = "isolated
flow, may be ticker-specific noise".

The score lives on `EnrichedEvent.sector_confirmation_score` and
feeds the combined score via `ScoringWeights.sector_confirmation`
(default 0.05, Track B 0.05 unchanged because squeeze precursors
are typically ticker-specific).

### Provider mapping

`UnusualWhalesSectorMapProvider` + `UnusualWhalesPeerFlowProvider` (Phase 3.9.10):

  - sector → `GET /api/stock/{ticker}/info` (`data.sector`). ETFs and indices
    have an empty sector and take `no_sector`.
  - peers → `GET /api/screener/stocks?sectors[]=<sector>&order=marketcap&order_direction=desc&issue_types[]=Common Stock`,
    i.e. top peers by market cap. `/api/stock/{sector}/tickers` is
    alphabetical, so it is not used.
  - peer flow → `GET /api/option-trades/flow-alerts?ticker_symbol=<peers>`
    over an hour bucket with epoch-second cursors, filtered to
    `[event_ts - window, event_ts]`. Direction comes from the option type
    plus the ask- vs bid-side premium; multi-leg alerts are neutral.

Membership is current as of the fetch, not point-in-time. Do not use it
for historical replay without a date-aware source.

### Default thresholds + tuning rationale

```yaml
m25:
  confirmation_window_minutes: 60        # peer flow lookback
  high_peer_count: 5                     # 5+ tickers = full score
  medium_peer_count: 3
  low_peer_count: 1
  high_score: 1.0
  medium_score: 0.7
  low_score: 0.3
  no_peers_score: 0.0
  no_membership_score: 0.5               # ticker → sector lookup failed
  provider_timeout_s: 3.0
  provider_cache_ttl_s: 300
```

`high_peer_count = 5` reflects "broad sector positioning" (most
liquid sectors have 50-200 tickers; 5 with simultaneous UOA is
genuinely distinctive). `low_peer_count = 1` is the "two ducks
spotted" threshold (single peer = light confirmation, not noise).

### Judgment-call ledger (Phase 3.4.5)

  - **Two-provider pattern** — distinct providers for lookup and
    data, both injected. Cleaner separation than a single provider
    with two methods; future modules with similar shape (M26 dark
    pool needs ticker → universe) follow this pattern.
  - **`no_membership_score = 0.5`** — ticker → sector lookup may
    fail for thinly-covered names. Distinct from `no_peers_score`
    (lookup succeeded but no peer flow). Telemetry uses these to
    flag data quality issues.
  - **60-minute confirmation window** — matches typical
    institutional positioning cycle. Hour-long windows let
    delayed-recognition flow appear; shorter windows miss it.
  - **No directional matching** — peer flow is counted regardless
    of call/put split. Could be tightened (require peer flow in
    same direction) but adds complexity for unclear gain;
    backtest may revisit.

---

## M26 — Dark Pool Confirmation (Phase 3.4.6)

### What it computes

A BOOLEAN flag (NOT a score) reflecting whether a qualifying
dark-pool print appeared near the option event. "Qualifying" means
volume above a configurable threshold AND timestamp within a
configurable window of the event.

The flag lives on `EnrichedEvent.has_dark_pool_confirmation`. It
appears in the combined-score formula via the labeler, not via
weights — confirmed dark-pool prints unlock specific labels
(SWEEP_BURST, HIGH_CONVICTION_SEQUENCE etc.) regardless of
combined score.

### Provider mapping

`UnusualWhalesDarkPoolProvider` (Phase 3.9.7) → `GET /api/darkpool/{ticker}?date=&newer_than=&older_than=&limit=500&order_by=premium&order=desc`,
fetched per hour bucket.

  - Prints are filtered to `[event_ts - window, event_ts]` with
    `trf_executed_at <= event_ts`; canceled prints are skipped.
  - `side_estimate` from the NBBO: `price >= ask` → above_ask, `price <= bid`
    → at_or_below_bid, otherwise midpoint.
  - An invalid NBBO → unknown. Prints not priced against the NBBO also →
    unknown: contingent, QCT, average-price, derivatively priced,
    extended-hours.

Live note: on SPY 8 of the 9 prints ≥ $5M in a sampled hour were
contingent/QCT, so M26 on SPY mostly lands in `direction_unclear`.

### Default thresholds + tuning rationale

```yaml
m26:
  confirmation_window_minutes: 30        # event ± 30 minutes
  min_print_volume: 100_000              # shares
  provider_timeout_s: 3.0
  provider_cache_ttl_s: 300
```

`confirmation_window_minutes = 30` (event ± 30 min) captures
"institutional buyer working an order across venues simultaneously".
Wider windows admit unrelated prints; narrower miss the
typical work-the-order pattern.

`min_print_volume = 100_000` shares is the institutional-significance
threshold for dark-pool prints — most dark-pool prints below this
size are routine, not informative.

### Judgment-call ledger (Phase 3.4.6)

  - **Boolean output, not score** — dark-pool confirmation either
    happens or it doesn't; a gradient would over-engineer the
    signal. The labeler reads the flag; downstream consumers see
    one bit.
  - **No M26 score field on EnrichedEvent or StoredSignal** — only
    the flag (`has_dark_pool_confirmation`). Phase 3.5 may add a
    score field for tunable weights, but Phase 3.4 keeps it minimal.
  - **Symmetric window (event ± 30 min)** — dark-pool prints can
    LEAD or LAG options flow (institutional working both legs
    simultaneously). Asymmetric windows assume one direction; the
    symmetric default doesn't.
  - **Volume threshold applies pre-window-filter** — prevents
    huge but unrelated prints (different ticker session) from
    triggering. Cost: small institutional prints below threshold
    miss confirmation. Trade-off favours fewer false positives.

---

## M27 — Opening/Closing OI Delta (Phase 3.4.7)

### What it computes

A score in `[0, 1]` reflecting whether the contract's open interest
is GROWING (opening flow / institutional accumulation) or SHRINKING
(closing flow / position unwind). High score = strong opening
(OI delta > 50%); low score = closing (OI delta < -10%).

The score lives on `EnrichedEvent.opening_closing_score`. Phase 3.4.7
treated it as internal/telemetry; **Phase 3.4.8 promoted it to a
domain field** because Module 28 is now a real consumer.

The score does NOT enter the combined score formula directly.
Instead it serves as the M28 filter input: M28 only validates
signals with `opening_closing_score >= min_m27_score_to_validate`
(default 0.7).

### Provider mapping

`UnusualWhalesOpenInterestProvider` (Phase 3.9.9) → `GET /api/option-contract/{OCC}/historic`.
Rows sit under `chains`, one per trading day, fetched once per contract
(the `date` query parameter is ignored live and not sent).

  - `at(when)` returns the latest row with `date <= ET date(when)`.
  - Live semantics are start-of-day. Row D is published pre-market and
    equals the OI after D-1's trading.
  - M27's prior/current pair therefore measures the OI built during D-1.
    Intraday opening cannot be observed in daily OI.

### Default thresholds + tuning rationale

```yaml
m27:
  strong_opening_threshold: 0.5          # >50% OI growth = strong
  moderate_opening_threshold: 0.10       # 10-50% growth = moderate
  closing_threshold: -0.10               # < -10% = closing
  strong_opening_score: 1.0
  moderate_opening_score: 0.7
  neutral_score: 0.5
  closing_score: 0.0
  new_strike_score: 1.0                  # prior_oi == 0 = treat as opening
  timeout_score: 0.5
  no_data_score: 0.5
  session_close_utc_hour: 21             # 16:00 ET DST = 21:00 UTC
  session_close_utc_minute: 0
  provider_timeout_s: 2.0
  provider_cache_ttl_s: 300
```

`strong_opening_threshold = 0.5` is the institutional-significance
line: 50% OI growth in a single day for an established contract
implies large new positioning, not routine churn. `moderate = 0.10`
captures the broader "fresh interest visible" band. Track B lowers
moderate to 0.05 (3.4.9.2) to catch stealthy accumulation.

`closing_threshold = -0.10` (10% OI shrinkage) is the symmetric
mirror — large enough to indicate genuine unwind, not just expiry
mechanics.

`new_strike_score = 1.0` handles `prior_oi == 0`: division by zero
prevented, semantic distinction preserved (this is genuinely new
positioning, not a "no data" case).

### Judgment-call ledger (Phase 3.4.7)

  - **Three "no data" states distinguished** —
    `new_strike` (prior_oi was 0; treat as full opening),
    `no_data` (provider returned None; neutral),
    `timeout` (provider didn't respond; neutral).
    Phase 3.5 backtest cost auditing relies on these distinctions —
    `no_data` often indicates UW coverage gaps for thin tickers.
  - **`prior_oi == 0` divide-by-zero check** — defensive but also
    semantic (new strike vs. data gap).
  - **Session close hardcoded to UTC, DST handled by hour/minute
    config** — `session_close_utc_hour = 21` is 16:00 ET in DST;
    operator must adjust to 22 (16:00 EST) outside DST.
    Trading-calendar overlay (Phase 3.5+) will handle this auto.
  - **Score promoted to domain field in 3.4.8.1** — was
    internal/telemetry in 3.4.7; M28 needed to filter signals,
    so the field was materialized on EnrichedEvent + StoredSignal.
    Idempotency-on-preset added at the same time.
  - **Timeout still writes score** — Phase 3.4.8 decision: timeout
    branch sets `event.opening_closing_score = timeout_score (0.5)`
    so the field is consistent across all branches. Without this,
    M28 couldn't distinguish "M27 timed out" from "M27 never ran".

---

## M28 — Next-Day OI Confirmation (Phase 3.4.8)

### What it computes

A score in `{0.0, 0.5, 1.0}` reflecting whether next-day OI
movement confirmed M27's opening-classification. Computed
POST-EVENT by a batch validator — for each StoredSignal from
prior session with `opening_closing_score >= 0.7`, fetch T+1
open interest, compare to event-time OI, and score:

  - delta > 0  → 1.0 (M27 was right; OI grew)
  - delta == 0 → 0.5 (ambiguous)
  - delta < 0  → 0.0 (M27 was wrong; OI shrank)

The score is written back to `StoredSignal.m28_confirmation_score`
via `BacktestStoreProtocol.update_signal_score()` — a Phase 3.4.8
breaking change that gives validators a write-back rail.

### Provider mapping

Same provider as M27. `next_day(trade_date)` returns the earliest `historic`
row dated after `trade_date`, so a Friday trade resolves to Monday's row.

### Default thresholds + tuning rationale

```yaml
m28:
  min_m27_score_to_validate: 0.7         # filter: only validate moderate+
  confirmed_score: 1.0
  ambiguous_score: 0.5
  closing_score: 0.0
  provider_timeout_s: 5.0                # batch tolerates slow endpoints
  batch_run_time_et: "09:31"             # 1 min after market open
  provider_cache_ttl_s: 86400            # T+1 OI is stable, 24h cache
```

`min_m27_score_to_validate = 0.7` filters down to signals that
warrant the cost of a T+1 OI fetch. Lowering it to 0.0 validates
EVERY signal at proportionally higher API cost; operator may
tune for backtest coverage.

`provider_timeout_s = 5.0` is HIGHER than M21-M27 (2.0-3.0)
because the batch job is not latency-critical — tolerate slow
endpoints for the price of completeness.

`batch_run_time_et = "09:31"` is one minute after market open;
CBOE has published prior-night settle OI by then.

### Judgment-call ledger (Phase 3.4.8)

  - **First non-PipelineStage module** — lives in
    `pipeline/validators/`, not `pipeline/stages/`. The lifecycle
    distinction (event-time vs batch) drives the package split.
  - **First Protocol breaking change since Phase 3.3** —
    `BacktestStoreProtocol.update_signal_score` added. Whitelist
    of allowed score names (ClassVar frozenset) prevents typos.
    Returns `bool` for matched-and-updated, raises `ValueError`
    on unknown score name.
  - **Signal-time prior_oi re-fetched per validation** — M27
    didn't persist its `prior_oi` baseline; M28 re-fetches via
    `provider.at(when=signal.timestamp)`. Provider TTL cache
    serves duplicates free if same-session.
  - **Pending state distinct from error** — provider returns None
    for `next_day()` → mark `m28_pending`, retry next batch run.
    Distinct from exceptions (counted under `errors`).
  - **Validator never raises on individual signal failure** —
    batch resilience: a single contract delisting must not abort
    the entire run.
  - **Post-event filter via Python, not SQL** — adding a SQL
    column for `opening_closing_score` would speed up filtering
    on large runs but requires Alembic migration. Phase 3.5+ may
    revisit.
  - **Far-future expiry (2099) used in smoke** — UW will return
    None for both at() and next_day() for synthetic SPY 2099,
    landing the smoke deterministically in `pending_no_data`.
    Smoke exercises wiring not branch coverage.

---

## Cross-cutting concerns

### Provider injection pattern

Every Phase 3.4 module receives its provider(s) via constructor
keyword arguments with default factories that return NoOp
implementations. Tests override; production wires real Phase 3.3
providers. This pattern was established in Phase 3.4.1 and held
through 3.4.8.

### `last_execution_metadata` telemetry

Every Phase 3.4 stage sets `self.last_execution_metadata` to a
dict-of-strings on each `enrich()` call. The orchestrator copies
this to `StageExecutionEntry.metadata` for downstream telemetry.
Branch labels are stable strings (`"strong_opening"`,
`"timeout"`, `"preset_skip"`, etc.) — operator-readable and
assert-friendly.

### Idempotency-on-preset

If the relevant sub-score field is already non-None at stage
entry, the stage short-circuits and records `branch=preset_skip`.
This makes the pipeline idempotent under retry: re-running a
partially-processed event is safe. M21-M23, M25, M27 follow this
pattern. M24 (penalty, no field), M26 (boolean), M28 (validator)
do not.

### Timeout protection

Every stage wraps provider calls in `asyncio.wait_for(...,
timeout=settings.provider_timeout_s)`. On timeout, the stage
logs a warning and falls back to a configurable score
(`timeout_score` or equivalent) with `branch=timeout` metadata.
Never raises on timeout — orchestrator continuity guaranteed.

### No hardcoded thresholds

Every threshold, score branch, and activation cutoff lives in
`CalibrationProfile` under `scoring.modules.<module>`. Tests
assert against profile-loaded values, never hardcoded constants.
This is the **calibration contract** for Phase 3.4 — Phase 3.5
backtest tuning happens in YAML, not code.

### Cross-module ScoreAdjustment rail

When a module needs to influence the COMBINED score (not its own
sub-score), it emits a `ScoreAdjustment` record. M24 was the first
consumer; the rail supports three target classes:

  - sub-score (e.g., `uoa_score`): M34 path
  - combined-score-pre (M24 + future): both `combined_score_pre`
    and `combined_score_pre_penalty` aliases
  - unrelated: silently ignored (forward-compat)

Direct mutation of cross-module sub-scores remains forbidden.

---

## Self-derived ThetaData providers (Phase 3.6.x, backtest-only, closed track)

Package `src/uoa_detector/sources/thetadata_derived/`, from the phase-3
lineage; it arrived with the Phase 5.0 merge. These providers compute
enrichment axes from locally downloaded ThetaData data instead of Unusual
Whales.
- They satisfy the same provider Protocols, so stage scoring is unchanged.
- Only `backtest/cell_runner.py::fusion_stages_with_thetadata` wires them
  (`backtest run-4cell --trades replay --chain-snapshots …`), with profile
  `profiles/v6_thetadata_confluence.yaml`.
- Nothing live uses them.
- The track is closed: EDGE REJECTED (`docs/phase-3.6-closeout.md`,
  `docs/phase-3.6-results.md`). Its data window is burned
  (`docs/INDEX.md` §6).

| Module | Classes | Serves | Data |
|---|---|---|---|
| `dealer_gamma.py` (3.6.1) | `ThetaDataDealerPositioningProvider` | `DealerPositioningProvider` → M21 | chain snapshots via `DailyChainSnapshotSource` |
| `chain.py` (3.6) | `ChainSnapshotSource` Protocol, `ChainContract`, `ChainAsOf` | point-in-time chain interface for the GEX provider; the as-of selection is the look-ahead guard | none |
| `snapshot_reader.py` (3.6.2) | `DailyChainSnapshotSource` | implements `ChainSnapshotSource` | `data/chain_snapshots/{TICKER}.parquet` from `scripts/download_chain_snapshots.py` |
| `chain_history.py` (3.6.4) | `ChainHistory` | per-contract OI/IV series and per-ticker ATM IV series for the IV and OI providers | same snapshots |
| `iv_oi.py` (3.6.4) | `ThetaDataIVHistoryProvider`, `ThetaDataOpenInterestProvider` | `IVHistoryProvider` → M24; `OpenInterestProvider` → M27 | `ChainHistory` |
| `price_action.py` (3.6.5) | `SpotSeries`, `ThetaDataPriceActionProvider` | `PriceActionProvider` → M23 | `data/spot_series/{TICKER}.parquet` from `scripts/compute_spot_series.py` |
| `catalyst_calendar.py` (3.6.6) | `CSVCatalystCalendarProvider` | `CatalystCalendarProvider` → M22, and M24's post-earnings gate | `data/earnings_calendar.csv` from `scripts/fetch_earnings_calendar.py` |
| `black_scholes.py` (3.6) | `gamma`, `price`, `implied_vol` | Black-Scholes math (r = 0) for the GEX provider and for IV inversion from the eod mid | none |

**Wiring** (`fusion_stages_with_thetadata`)
- **Always self-derived.** M21, M24 and M27 whenever `--chain-snapshots` is
  set.
- **Optional axes.** M23 needs `--spot-series`. M22, and M24's catalyst
  gate, need `--catalyst-calendar`. Without those flags the stages keep
  their NoOp providers.
- **Always NoOp.** M25 (sector) and M26 (dark pool).
- **M37.** The replay producer feeds `data/medians_bulk.csv`
  (`scripts/compute_medians.py`) through `CSVMedianTradeSizeProvider`,
  outside this package.

**Profile and verdict**
- `v6_thetadata_confluence` inherits `v5_gamma_squeeze` and overrides one
  leaf: M21 `short_gamma_threshold = 0` (sign-based).
- The 3.6 closeout records that M24 is only a conditional penalty and M27
  feeds M28, not the combined score. Wiring them did not move the verdict.

**Stale docstring.** `snapshot_reader.py` names
`scripts/compute_chain_snapshots.py`; the script in the repo is
`scripts/download_chain_snapshots.py`.

---

## Non-pipeline consumers (webapp)

Two background tasks in the FastAPI app (`webapp/main.py` lifespan) call
Unusual Whales outside the CLI and backtest paths.
- Both start only when `LIVE_TICKERS`, `UNUSUAL_WHALES_API_KEY` and
  `DATABASE_URL` are set (`webapp/worker.py::live_config_from_env`).
- Each runs under a supervisor that restarts it 30 s after it exits or
  raises.
- Runtime and variables: `docs/DATA_INTEGRATION.md` §8–§10.

### `webapp/worker.py`: live signal worker

- **Source.** `UnusualWhalesFlowPollSource`
  (`sources/unusual_whales/flow_poll.py`) polls per-ticker
  `GET /api/stock/{t}/flow-alerts`. This keeps parity with the phase-3
  production path (`docs/phase-5.0-merge-acceptance.md` §3.8). The port to
  `/api/option-trades/flow-alerts` is a registry item: it changes the
  event-id scheme and starts firing the wide_spread penalty on live cards.
- **Stages.** `build_live_stage_pipeline(client, profile,
  degrade_transient_errors=True)` gives the same stages and order as
  `fusion_stages_with_uw`, with one shared catalyst provider.
  - Degrading wrappers for M21, M22, M24, M25, M26 and M27 map three error
    families to each protocol's documented no-data return, log a WARNING
    and increment a counter: UW rate-limit errors (daily limit included),
    transient errors and an open circuit breaker.
  - `UnusualWhalesAuthError` and programming errors (`TypeError`,
    `ValueError`) still raise.
  - The builder's default (`False`) leaves the screener and backtests
    unchanged. `fusion_stages_with_uw` stays for the replay path.
  - Until the stage-level `provider_error` branch lands (registry), a
    degraded axis shows the stage's no-data branch in decision records.
- **Store.** `SqliteBacktestStore(url, flush_threshold=1, replay_safe=True)`
  (Phase 4.42, 4.43). The poll source re-emits its last 10 minutes of alerts
  on every rebuild. The replay-safe store skips a signal whose
  `(run_id, event_id)` is already stored, and discards a batch whose commit
  failed.
- **Run id.** `live-YYYY-MM-DD` (UTC date), adopted again on a same-day
  restart.
- **Profile.** `profiles/v5_default.yaml`.
- **Budget guards.** RTH-only polling, a 60 s poll floor and an 1800 s
  daily-limit backoff (`docs/DATA_INTEGRATION.md` §10).

### `webapp/gamma_live.py`: gamma / vol board

- **Refresh.** Every 240 s during RTH, per ticker, it calls
  `GET /api/stock/{t}/greek-exposure/strike`, `/api/stock/{t}/iv-rank`,
  `/api/earnings/{t}` and `/api/stock/{t}/ohlc/1d`, plus
  `UnusualWhalesCatalystCalendarProvider.next_catalyst`. Outside RTH it
  sleeps 300 s between checks.
- **Storage.** Results are upserted into the `gamma_regime` table read by
  the webapp's `/gamma` page. On startup the loop drops and recreates that
  table.
- **`net_gex` is share gamma.** It is the sum of `call_gex + put_gex` over
  the whole book, not USD per 1% move (see the 3.9 contract §3.2).
  - The board uses only its sign, the flip and the call/put walls, all
    unit-invariant.
  - The flip is the cumulative zero crossing nearest spot, over strikes
    within 0.6–1.4 × spot.
  - `net_gex` is not comparable to M21's `net_gamma_dollars` or to any
    profile threshold. Changing its units is a registry item.
- **Realized vol.** The IV-vs-realized read uses the last 21 regular-session
  daily closes from `ohlc/1d`, ordered by date.
  `webapp/pricing.latest_close` (journal prices) uses the newest regular
  close (contract §3.9).
- **Client use.** It calls the UW client directly (no provider, no stage)
  and builds a new client every round; client reuse is a registry item.

---

## Deferrals to Phase 3.5+

Items flagged during Phase 3.4 but explicitly NOT addressed:

  - **Trading-calendar overlay** — half-day handling (M23), calendar
    days vs trading days (M22), prior-session-close holiday handling
    (M27). Currently uses calendar-day approximations; Phase 3.5
    introduces an explicit trading-calendar provider.
  - **TTLCache instrumentation** — providers have TTL caches but
    don't expose hit/miss counts. Phase 3.5 metrics dashboard will
    surface these for operator monitoring.
  - **`opening_closing_score` SQL column** — currently lives in
    `full_record_json`. Adding a dedicated column would speed up
    M28 batch filtering on large runs; Alembic migration cost
    deferred until backtest data shows it's needed.
  - **`next_day_oi_confirmed` boolean persistence** — M28 writes
    `m28_confirmation_score` but not the derived boolean field.
    Downstream consumers can derive (`score == 1.0`); explicit
    write deferred until a consumer needs it.
  - **M22 event-type weighting** — currently type-blind. Backtest
    may show earnings vs FDA vs FOMC have type-specific edge
    differences worth weighting.
  - **M25 directional peer matching** — currently counts peers
    regardless of call/put split. Tightening to same-direction may
    or may not help; backtest will tell.
  - **M26 score field** — currently boolean only. Score gradient
    (volume above threshold scaled to [0,1]) deferred unless
    boolean is shown insufficient.
  - **M28 SQL filter via dedicated column** — see opening_closing
    above.

---

## Cumulative test count

| Phase | Module | Sub-commits | New tests | Cumulative |
|-------|--------|------------:|----------:|-----------:|
| 3.4.1 | M21 dealer gamma | 4 | 54 | 1067 |
| 3.4.2 | M22 event calendar | 4 | 44 | 1110 |
| 3.4.3 | M23 price confirmation | 4 | 59 | 1168 |
| 3.4.4 | M24 IV exhaustion | 4 | 45 | 1212 |
| 3.4.5 | M25 sector peer | 3 | 36 | 1247 |
| 3.4.6 | M26 dark pool | 3 | 34 | 1281 |
| 3.4.7 | M27 OI delta | 3 | 37 | 1318 |
| 3.4.8 | M28 next-day OI | 4 | 73 | 1390 |
| 3.4.9 | Closeout (in progress) | 5 | (varies) | 1391+ |

Per-module integration smokes are gated on credentials and skip
without them. Skipping them via `pytest -m "not integration"` runs
the unit-only suite in ~60 seconds.

---

## Where to go next

  - Current truth across both lineages, doc types, Phase 5.x registry:
    `docs/INDEX.md`
  - Current UW endpoints: `docs/phase-3.9-uw-endpoint-correction-acceptance.md`
  - Phase 3.4 acceptance contract: `docs/phase-3.4-acceptance.md`
  - Phase 3.3 acceptance contract: `docs/phase-3.3-acceptance.md`
    (data feeds and providers consumed by Phase 3.4)
  - Backtest framework reference: `docs/BACKTEST.md`
  - Data integration reference: `docs/DATA_INTEGRATION.md`

For module-specific debugging, start with the module's
integration smoke (`tests/integration/test_m{NN}_smoke.py`) — it
exercises the live wiring against the real provider, gated on
credentials.
