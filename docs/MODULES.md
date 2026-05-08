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

## M21 — Dealer Gamma Exposure (Phase 3.4.1)

### What it computes

A score in `[0, 1]` reflecting how exposed the dealer book is to
gamma reflexivity around the current spot. High score = dealers are
short-gamma AND spot is near the gamma flip point AND recent
acceleration in net gamma → reflexive hedging cycle is plausible.

The score lives on `EnrichedEvent.gamma_score` and feeds the combined
score via `ScoringWeights.gamma` (default 0.10, Track B 0.25).

### Provider mapping

`UnusualWhalesDealerGammaProvider` → UW endpoint
`/api/option/{ticker}/gex/strikes`. Returns total dealer gamma in
USD-per-1%-spot-move and per-strike net gamma. The provider derives:

  - `total_dealer_gamma`: scalar in USD/1%
  - `gamma_flip_strike`: spot price at which net dealer gamma flips
    sign (zero crossing of the per-strike curve)
  - `nearest_strike_gamma`: dealer gamma at the strike of the event

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

`UnusualWhalesEventCalendarProvider` → UW endpoints
`/api/earnings/upcoming`, `/api/fda-calendar/upcoming`,
`/api/economic-calendar/upcoming`, `/api/dividends/upcoming`. Returns
a list of `CalendarEvent` records with `event_type`,
`scheduled_for_date`, and optional `confirmation_status`.

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

`ThetaDataPriceActionProvider` → ThetaData endpoint
`/v2/hist/stock/ohlc?ivl=60000` (1-minute bars). Returns OHLC for
the post-event window, from which the provider extracts the high
(for call confirmation) or low (for put confirmation).

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

### Judgment-call ledger (Phase 3.4.3)

  - **ThetaData provider, not UW** — UW exposes flow not OHLC; spot
    OHLC is ThetaData's domain. M23 is the only Phase 3.4 module
    consuming ThetaData; the rest go through UW.
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

`UnusualWhalesIVHistoryProvider` → UW endpoint
`/api/option/{ticker}/iv-rank`. Returns 30-day IV percentile for
the underlying. Provider derives the percentile rank for the
event's IV against this 30-day window.

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

`UnusualWhalesSectorFlowProvider` → UW endpoint
`/api/sector-flow/{sector}`. Returns aggregated UOA counts for
the sector over recent windows (1h, 4h, 1d). Plus
`UnusualWhalesSectorMembershipProvider` →
`/api/ticker/{ticker}/sector` for the ticker → sector mapping.

This is the **two-provider pattern** introduced in Phase 3.4.5:
one provider supplies the lookup (sector membership), the other
supplies the data (sector flow). M25 wires both via constructor.

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

`UnusualWhalesDarkPoolProvider` → UW endpoint
`/api/darkpool/{ticker}`. Returns recent off-exchange prints with
volume, timestamp, and price. M26 filters by volume threshold and
window.

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

`UnusualWhalesOpenInterestProvider` → UW endpoint
`/api/option-contract/{ticker}/{strike}/{expiry}/{type}/oi`. Returns
`OpenInterestSnapshot(as_of, open_interest)`. M27 makes two calls:
one for prior session close, one for the current event timestamp.
Computes `oi_delta_pct = (current - prior) / prior`.

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

Same provider as M27: `UnusualWhalesOpenInterestProvider`. M28
calls `provider.next_day(ticker, strike, expiry, option_type,
trade_date)` — Phase 3.3.3 method that returns T+1 OI specifically.

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

  - Phase 3.4 acceptance contract: `docs/phase-3.4-acceptance.md`
  - Phase 3.3 acceptance contract: `docs/phase-3.3-acceptance.md`
    (data feeds and providers consumed by Phase 3.4)
  - Backtest framework reference: `docs/BACKTEST.md`
  - Data integration reference: `docs/DATA_INTEGRATION.md`

For module-specific debugging, start with the module's
integration smoke (`tests/integration/test_m{NN}_smoke.py`) — it
exercises the live wiring against the real provider, gated on
credentials.
