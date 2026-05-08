# Phase 3.4 — Module implementations (M21-M28)

This is the working acceptance doc for Phase 3.4. Phase 3.3 wired the
data feeds (ThetaData adapter + UW adapter + 6 providers); Phase 3.4
turns the eight currently-stubbed enrichment stages (M21-M28) into
real implementations that consume those providers.

This is the phase where **edge becomes measurable**. After 3.4,
Phase 3.5 runs the first real backtest — the 4-cell combinatorial
runner against tier-2 historical replay — and produces the answer
to "is there an edge in Track B gamma squeeze precursor signals."

Phase 3.4 has nine sub-phases (one per module + a closeout). Total
estimated scope: 30-50 commits, 2000-3500 LoC, 150-250 new tests.

---

## Approved decisions (locked in before implementation)

1. **Each module is one sub-phase, separately committable.** A
   module's commit may span multiple sub-commits internally
   (provider wiring, score computation, edge cases, tests) but the
   sub-phase is one bisectable unit ending in green.

2. **No score change is hardcoded.** Every threshold, weight, and
   activation cutoff lives in `CalibrationProfile` under
   `scoring.modules.<module_name>`. Test pins assert the profile is
   read, not hardcoded values.

3. **Each module is a `PipelineStage`** (already-defined Phase 2
   contract). It receives a `PipelineEvent`, mutates `sub_scores`
   in-place, and may emit `ScoreAdjustment` records. It MUST NOT
   mutate `combined_score_pre/post` directly — that's the
   `combined_score_combiner` stage's job downstream.

4. **Providers are Phase 3.3 surface; modules consume, don't
   re-implement.** A module never makes its own HTTP calls. It
   receives a `Provider` instance via constructor injection.
   Tests mock the provider; integration smoke tests gate on env
   keys.

5. **Idempotency-on-preset.** Each stage starts with
   `if event.<my_score> is not None: return` — a stage that has
   already produced its score (e.g., from a hot-swap profile run)
   is not recomputed. This pattern is pinned in Phase 2.

6. **`ScoreAdjustment` for additive bonuses, not direct sub-score
   mutation.** When M34's "ISO sweep + above-ask + cluster" combo
   should add bonus to `uoa_score`, M34 emits a `ScoreAdjustment`
   that targets `uoa_score`. Direct mutation of another module's
   score is forbidden. This is pinned in Phase 2's `M34` test.

7. **No semantic interpretation of UW labels.** Even when UW marks
   a print as "sweep", our M34 runs its own classifier
   independently. UW labels go into `RawPrint.metadata` for audit
   and (later) cross-validation, not into score logic. This
   preserves edge definition independence.

8. **Module timeouts are explicit.** Each provider call wraps in
   `asyncio.wait_for(timeout=profile.modules.<name>.provider_timeout_s)`.
   Default 2.0s. On timeout: log + emit zero score + continue.
   System never blocks on a slow provider.

9. **Cache hit/miss telemetry visible.** Each module records
   provider cache hit-rate in `StageExecutionRecord.metadata` so
   Phase 3.5 backtests can audit provider load. (Not for
   correctness, for cost/performance monitoring.)

10. **Module precedence on conflicting signals: explicit, profiled.**
    When M21 (gamma squeeze setup) and M22 (post-earnings flow)
    both fire on same event, profile YAML pins their interaction
    (additive, suppressive, mutually-exclusive). Decision: Phase
    3.4 wires modules independently; precedence/conflict resolution
    is a Phase 3.5 calibration question once we see real signal
    interactions in backtest.

---

## Phase 3.4.1 — M21: Dealer gamma exposure score

The hypothesis: when dealers are net-short gamma in a name and
spot moves toward the gamma flip point, dealer hedge demand creates
reflexive flow that amplifies the move. M21 quantifies how
"squeeze-prone" the dealer position is right now.

**Provider:** `DealerPositioningProvider` (UW
`/api/stock/{ticker}/greek-exposure`).

**Computation:**
- Fetch dealer net gamma exposure for ticker, current as of
  event timestamp
- Fetch zero-gamma flip strike (from same provider)
- Compute spot-to-flip distance as fraction of underlying:
  `distance_pct = abs(spot - flip_strike) / spot`
- Score:
  - `gamma_score = 1.0` if dealer_net_gamma < `profile.modules.m21.short_gamma_threshold`
    AND `distance_pct < profile.modules.m21.flip_proximity_pct` (default 0.03 = 3%)
  - `gamma_score = 0.5` if either condition met but not both
  - `gamma_score = 0.0` otherwise
- Score is bounded [0.0, 1.0].

**Profile section** `scoring.modules.m21`:
```yaml
short_gamma_threshold: -50_000_000  # USD; dealer net gamma below = short
flip_proximity_pct: 0.03            # spot within 3% of flip = ripe
provider_timeout_s: 2.0
provider_cache_ttl_s: 300           # 5 min — gamma updates intraday
```

**Edge cases:**
- Provider returns no data (new IPO, illiquid name) → score = None,
  log "no gamma data available", do not raise
- Provider timeout → score = 0.0, log warning
- `distance_pct > 0.20` → score = 0.0 (too far for hedge flow)

**Done when:**
- 12+ unit tests covering happy path, threshold boundaries, missing
  data, timeout, idempotency-on-preset
- Profile schema test asserting `m21` section reads correctly
- Integration smoke test (gated by `UNUSUAL_WHALES_API_KEY`):
  fetches gamma for SPY, asserts score in [0.0, 1.0]

---

## Phase 3.4.2 — M22: Event calendar score

The hypothesis: pre-event option flow (earnings, FDA, Fed) carries
information; post-event flow is noise. M22 down-weights signals
near recent catalysts and up-weights signals before scheduled ones.

**Provider:** `CatalystCalendarProvider` (UW
`/api/earnings/upcoming`, `/api/fda-calendar/upcoming`).

**Computation:**
- Fetch all scheduled catalysts for ticker within ±30 days of
  event timestamp
- For each catalyst, compute days_to or days_since
- Score:
  - `event_score = 0.0` if any catalyst within `profile.modules.m22.post_event_blackout_days`
    of past (default 1 day)
  - `event_score = 1.0` if catalyst within `profile.modules.m22.pre_event_window_days`
    of future (default 14 days) AND DTE > days_to_catalyst (option survives the event)
  - `event_score = 0.5` if catalyst exists in window but DTE < days_to_catalyst
    (option expires before catalyst — speculative position)
  - `event_score = 0.3` if no catalyst in window — neutral, not penalized

**Profile section** `scoring.modules.m22`:
```yaml
post_event_blackout_days: 1
pre_event_window_days: 14
provider_timeout_s: 2.0
provider_cache_ttl_s: 3600  # 1 hour — calendar changes infrequently
```

**Edge cases:**
- Multiple catalysts overlap → use closest in time
- Provider returns empty → `event_score = 0.3` (neutral fallback,
  not zero — absence of data ≠ absence of edge)
- Catalyst on same day as event → treat as post-event blackout

**Done when:**
- 14+ unit tests including all branch cases of the score function
- Profile schema test
- Integration smoke (fetch SPY upcoming earnings, assert structure)

---

## Phase 3.4.3 — M23: Price confirmation score

The hypothesis: option flow without underlying price confirmation
is suspicious (could be hedge, dealer flow, or stale signal).
Strong directional spot move alongside option print = high
conviction.

**Provider:** ThetaData spot price snapshot (already in Phase 3.3.2
`ThetaDataClient`). Adapter wrapping required: `PriceActionProvider`
that exposes `get_intraday_price_movement(ticker, timestamp,
lookback_minutes)`.

**Computation:**
- Fetch spot price at event timestamp and 30-minute lookback
- Compute `move_pct = (spot_now - spot_then) / spot_then`
- For call options: `confirmed = move_pct > profile.modules.m23.confirmation_pct`
- For put options: `confirmed = move_pct < -profile.modules.m23.confirmation_pct`
- Score:
  - `price_confirmation_score = 1.0` if confirmed
  - `price_confirmation_score = 0.3` if move is in opposite direction
    AND magnitude > confirmation_pct (contrarian flow — could be hedge)
  - `price_confirmation_score = 0.5` otherwise (no confirmation, no contradiction)

**Profile section** `scoring.modules.m23`:
```yaml
lookback_minutes: 30
confirmation_pct: 0.005      # 0.5% spot move within window
provider_timeout_s: 2.0
provider_cache_ttl_s: 60     # 1 min — intraday data
```

**Edge cases:**
- Lookback crosses session boundary → reduce lookback to session start
- Spot data missing → `price_confirmation_score = 0.5` (neutral)
- Lookback during half-day session — adjust automatically

**Done when:**
- 15+ unit tests including session boundary cases
- New `PriceActionProvider` Protocol implementation in
  `src/uoa_detector/providers/price_action.py` (file already exists
  as stub; replace with real)
- Integration smoke (gated by `THETADATA_API_KEY`)

---

## Phase 3.4.4 — M24: IV exhaustion score

The hypothesis: when IV rank is already very high, the option is
expensive AND the move-priced-in is large. Edge is greater when
buying when IV is low/moderate.

**Provider:** `IVHistoryProvider` (UW
`/api/stock/{ticker}/iv-rank`).

**Computation:**
- Fetch IV rank percentile for ticker (0-100 scale)
- Score:
  - `iv_score = 1.0` if IV rank < `profile.modules.m24.low_iv_threshold` (default 30)
  - `iv_score = 0.7` if IV rank between low_iv and `mid_iv_threshold` (default 60)
  - `iv_score = 0.3` if IV rank between mid_iv and `high_iv_threshold` (default 80)
  - `iv_score = 0.0` if IV rank > high_iv_threshold

This is also where the **post-earnings IV spike penalty** is
emitted via `ScoreAdjustment` (target=`combined_score_pre`,
delta=-0.4, reason="IV rank > 80 + earnings within 1 session").

**Profile section** `scoring.modules.m24`:
```yaml
low_iv_threshold: 30
mid_iv_threshold: 60
high_iv_threshold: 80
post_earnings_iv_penalty: -0.4
post_earnings_iv_penalty_threshold: 80
provider_timeout_s: 2.0
provider_cache_ttl_s: 600     # 10 min — IV rank changes slowly
```

**Edge cases:**
- New listing with no IV history → `iv_score = 0.5`, no penalty
- Stale IV data (>1 day old) → log warning, use anyway
- Provider returns rank > 100 (data error) → clamp to 100

**Done when:**
- 13+ unit tests including penalty emission
- Cross-module test: M24 + M22 (recent earnings + high IV) emits
  combined penalty correctly
- Integration smoke

---

## Phase 3.4.5 — M25: Sector peer score

The hypothesis: sector-wide flow corroborates single-name signal.
If NVDA call-buying coincides with AMD/AVGO call-buying, the trade
thesis is sector-wide and more likely to play out.

**Providers:** `SectorMapProvider` (ticker → sector) +
`PeerFlowProvider` (recent flow across sector).

**Computation:**
- Look up sector for event ticker
- Fetch flow for top 5 peer tickers in sector over last
  `profile.modules.m25.peer_window_minutes` (default 30 min)
- Compute `peer_alignment_pct` = fraction of peers showing
  same-direction unusual flow (calls if event is call, puts if put)
- Score:
  - `sector_score = 1.0` if peer_alignment_pct > 0.6
  - `sector_score = 0.7` if 0.4 - 0.6
  - `sector_score = 0.3` if 0.2 - 0.4
  - `sector_score = 0.0` if < 0.2 (sector peers showing OPPOSITE
    direction → contrarian, not corroborating)

**Profile section** `scoring.modules.m25`:
```yaml
peer_window_minutes: 30
peer_count: 5
strong_alignment_threshold: 0.6
moderate_alignment_threshold: 0.4
weak_alignment_threshold: 0.2
provider_timeout_s: 3.0     # higher; multi-ticker fetch
provider_cache_ttl_s: 60
```

**Edge cases:**
- Ticker has no sector mapping (rare ETFs, foreign listings) →
  `sector_score = 0.5`, log "no sector"
- Peer list has fewer than 5 names (small sectors) → use what's
  available, normalize by count
- All peers have zero flow → `sector_score = 0.5` (neutral)

**Done when:**
- 16+ unit tests including small-sector and missing-sector cases
- Two providers wired (sector_map + peer_flow), both mocked in
  unit tests
- Integration smoke for both providers

---

## Phase 3.4.6 — M26: Dark pool corroboration

The hypothesis: large dark pool prints just before public option
flow indicate institutional positioning. Public flow alone could
be a tail; dark pool + flow = firm conviction.

**Provider:** `DarkPoolPrintProvider` (UW
`/api/darkpool/{ticker}`).

**Computation:**
- Fetch dark pool prints for ticker within last
  `profile.modules.m26.dark_pool_lookback_minutes` (default 60)
- Filter by minimum size: `profile.modules.m26.min_print_size_usd`
  (default $5M)
- Score:
  - `dark_pool_score = 1.0` if any qualifying print AND direction
    matches event (call event + bullish DP, put event + bearish DP)
  - `dark_pool_score = 0.5` if any qualifying print but direction
    unclear (DP doesn't carry side label)
  - `dark_pool_score = 0.2` if no qualifying prints

**Profile section** `scoring.modules.m26`:
```yaml
dark_pool_lookback_minutes: 60
min_print_size_usd: 5_000_000
provider_timeout_s: 2.0
provider_cache_ttl_s: 30
```

**Edge cases:**
- DP data delayed >5 min relative to event → log warning, use anyway
- Multiple prints, mixed directions → use largest by notional

**Done when:**
- 12+ unit tests
- Integration smoke (fetch SPY recent dark pool prints)

---

## Phase 3.4.7 — M27: Opening/closing OI delta

The hypothesis: open interest changes confirm whether option flow
opened new positions or closed existing ones. Opening flow = fresh
conviction; closing flow = profit-taking or stop-out.

**Provider:** `OpenInterestProvider` (UW
`/api/stock/{ticker}/oi-change`).

**Computation:**
- Fetch OI change for the contract (ticker + strike + expiry)
  between previous session close and event timestamp
- Compute `oi_delta_pct = (current_oi - prior_oi) / prior_oi`
- Score:
  - `opening_closing_score = 1.0` if `oi_delta_pct > 0.5`
    (significant new OI — opening)
  - `opening_closing_score = 0.7` if `oi_delta_pct between 0.1 and 0.5`
  - `opening_closing_score = 0.5` if `oi_delta_pct between -0.1 and 0.1`
    (neutral)
  - `opening_closing_score = 0.0` if `oi_delta_pct < -0.1`
    (closing — likely profit-take)

**Profile section** `scoring.modules.m27`:
```yaml
strong_opening_threshold: 0.5
moderate_opening_threshold: 0.1
closing_threshold: -0.1
provider_timeout_s: 2.0
provider_cache_ttl_s: 300
```

**Edge cases:**
- New strike/expiry — no prior OI data → `opening_closing_score = 1.0`
  (treat as opening, since OI change from zero is by definition new)
- Stale OI (provider returns same value as yesterday for live data) →
  log warning, use anyway

**Done when:**
- 12+ unit tests
- Integration smoke

---

## Phase 3.4.8 — M28: Next-day OI confirmation

The hypothesis: a trade that genuinely opened new positions will
show OI increase the *next* trading day at the open. A trade that
was actually a closer (or a wash) will show no OI change or decrease.
M28 retroactively validates M27's "opening" classification with T+1
data.

This is **the only retrospective module** — its score is computed
the next morning, not at event time. It writes back to the
`StoredSignal` record from the prior day.

**Provider:** `OpenInterestProvider.next_day_change(contract,
prior_session_close_oi)` — already specced in Phase 3.3.3.

**Computation:**
- Run as nightly batch (`scripts/run_m28_overnight.py`) at 09:31 ET
  next session day
- For each StoredSignal from prior session with `m27_score >= 0.7`
  (i.e., M27 said "opening"):
  - Fetch T+1 open OI for that contract
  - Compute `actual_oi_delta = next_day_open_oi - prior_session_close_oi`
  - If `actual_oi_delta > 0`: M27 was right → `m28_confirmation_score = 1.0`
  - If `actual_oi_delta == 0`: ambiguous → `m28_confirmation_score = 0.5`
  - If `actual_oi_delta < 0`: M27 was wrong → `m28_confirmation_score = 0.0`
- Update StoredSignal in BacktestStore with new score
- This is **post-hoc**: doesn't affect signal at event time, but
  Phase 3.5 backtest uses it to weight signals by reliability

**Profile section** `scoring.modules.m28`:
```yaml
provider_timeout_s: 5.0      # batch job, can wait
batch_run_time_et: "09:31"
```

**Edge cases:**
- T+1 OI provider returns no data (data delayed) → retry next day,
  flag signal as `m28_pending`
- Signal contract expired before T+1 (DTE was 0 at event) → skip,
  cannot confirm
- Prior day was non-trading day → skip, cannot confirm

**Done when:**
- 10+ unit tests for the batch logic
- New script `scripts/run_m28_overnight.py`
- BacktestStore method `update_signal_score(signal_id, score_name, value)`
  added (small breaking change to BacktestStoreProtocol — flag this in
  commit message)
- Integration smoke (gated by UW key)

---

## Phase 3.4.9 — Closeout

This is the final sub-phase: wire all eight modules into the default
pipeline, update profile YAML, run end-to-end smoke against
synthetic source, and update docs.

**Scope:**
- `pipeline/orchestrator.py` default stage list updated to include
  M21-M28 in correct order (M21-M27 at event time, M28 retrospective)
- `profiles/v5_default.yaml` updated with all module sections
- `profiles/v5_gamma_squeeze.yaml` Track B specific tunings
- End-to-end test: synthetic source → all 8 enrichment stages run
  → decision label produced → record persisted
- `docs/MODULES.md` written: per-module summary + provider mapping
  + tuning rationale

**Done when:**
- All 8 modules in default pipeline, behavior change visible
  (signal labels change vs Phase 3.3 baseline — this is expected
  and pinned in a new "module integration test")
- 1100+ tests passing total (extrapolating from current 1014)
- mypy --strict + ruff clean
- `MODULES.md` shipped

---

## Cross-cutting acceptance — applies to every commit in 3.4.x

- `pytest -q` green at every commit
- `mypy --strict` clean across all source files
- `ruff check .` clean
- No threshold/weight value hardcoded — every numeric in profile
- Every module has constructor injection of its provider (no
  module instantiates its own UW client)
- Every module has timeout protection on provider calls
- Every module supports idempotency-on-preset
- Stage tests use `MockProvider` patterns; integration smoke tests
  gated by env keys
- `BacktestStoreProtocol` from Phase 3.3 unchanged except for M28's
  `update_signal_score()` addition (only Phase 3.4.8 adds this,
  flagged in commit)

---

## What Phase 3.4 explicitly does NOT do

- Run real backtest (Phase 3.5)
- Tune profile weights/thresholds against backtest results (Phase 3.5+)
- Add new modules beyond M21-M28 (out of scope; Phase 4+)
- Live observer mode integration (already in Phase 3.3.5; this
  phase doesn't change it)

---

## What you (Berkay) need to do before Phase 3.4 starts

1. UW API key is already obtained (Phase 3.3 prep).
2. Run UW smoke tests locally to confirm key works:
   ```
   echo "UNUSUAL_WHALES_API_KEY=<your_key>" >> .env
   uv run pytest tests/integration/test_unusual_whales_smoke.py -v
   ```
   Expected: 8 pass (was 8 skip without key).
3. ThetaData Pro key needed for M23 smoke test only (Phase 3.4.3).
   Can be deferred until that sub-phase if not yet acquired.
4. Decide: is profile `v5_default.yaml` the production target, or
   does Track B (`v5_gamma_squeeze.yaml`) get its own per-module
   tunings before Phase 3.5? Decision needed by Phase 3.4.9; can
   be deferred until then.

---

## Calibration philosophy reminder

Phase 3.4 implements the **scoring functions** with **default
thresholds**. These defaults are educated guesses, not optimized
values. Phase 3.5 runs the 4-cell combinatorial backtest against
these defaults; if results are weak, Phase 3.5+ involves tuning
thresholds in profile YAML and rerunning the backtest.

The falsification discipline pinned in Phase 3.2.4 still applies:
the 4-cell comparison report has 4 partial-outcome scenarios that
REJECT Formülasyon A. We do not adjust those scenarios after seeing
results. Profile threshold tuning happens before backtest comparison,
not after.

---

This document is the contract for Phase 3.4. It will not be
revised mid-implementation; if a decision needs revisiting, that
discussion happens between sub-phases, not within them.

---

## Phase 3.4 closeout summary (added Phase 3.4.9.5)

This section is appended after Phase 3.4 completes. Following the
Phase 3.3.6 pattern, it captures the final state — sub-phase
summary, totals, judgment-call themes, and lokal verification
recipe — without revising the contract above.

### Sub-phase summary

| Sub-phase | HEAD | Tests | Sub-commits | Key delivery |
|---|---|---|---|---|
| 3.4.1 — M21 dealer gamma | `14829fe` | 1067 + 19 | 4 | `DealerGammaStage`; provider 3-field model; MAX-of-two-heuristics scoring; idempotency-on-preset pattern |
| 3.4.2 — M22 event calendar | `fb66f93` | 1110 + 19 | 4 | `EventCalendarStage`; 3-source aggregation (earnings/FDA/FOMC); pre/post-event window; calendar-day approximation flagged |
| 3.4.3 — M23 price confirmation | `3078521` | 1168 + 19 | 4 | `PriceConfirmationStage`; ThetaData OHLC integration (only ThetaData consumer in Phase 3.4); 3-state no-data semantics |
| 3.4.4 — M24 IV exhaustion | `9178abe` | 1212 + 19 | 4 | `IVExhaustionStage`; first cross-module penalty via `ScoreAdjustment`; 80th-percentile threshold; binary penalty |
| 3.4.5 — M25 sector peer | `2835f5c` | 1247 + 19 | 3 | `SectorPeerStage`; two-provider pattern (membership + flow); 5/3/1 peer-count tiers |
| 3.4.6 — M26 dark pool | `030d634` | 1281 + 19 | 3 | `DarkPoolStage`; boolean flag (no score); symmetric ±30min window; 100K-share volume threshold |
| 3.4.7 — M27 OI delta | `83c89d7` | 1318 + 19 | 3 | `OpeningClosingStage`; 3-state no-data semantics; prior_oi==0 divide-by-zero guard; new_strike branch distinct from no_data |
| 3.4.8 — M28 next-day OI | `6f8b5a9` | 1390 + 20 | 4 | `M28Validator` (FIRST non-PipelineStage module); `BacktestStoreProtocol.update_signal_score` BREAKING; `scripts/run_m28_overnight.py` CLI; `opening_closing_score` field promoted |
| 3.4.9 — Closeout | this | 1391 + 20 | 5 | Polygon/IBKR stub retirement; Track B M21+M27 overrides; end-to-end synthetic pipeline test; `docs/MODULES.md`; this closeout summary |

### Phase 3.4 totals

  - 9 sub-phases, 32 sub-commits (all bisectable; per-commit pytest
    sweep confirmed at every closeout)
  - 1391 tests green + 20 skipped (1 M21 + 1 M22 + 1 M23 + 1 M24
    + 1 M25 + 1 M26 + 1 M27 + 1 M28 integration smokes gated on UW
    key, 8 grouped UW smokes, 3 ThetaData smokes, 1 live_market;
    all gated by API keys + market hours where applicable)
  - 111 source files (was 113 before 3.4.9.1 stub deletion), mypy
    --strict + ruff clean across the surface
  - ~10000 LoC added across `src/`, `tests/`, `scripts/`, `docs/`
  - ~150 documented judgment calls in commit messages (search
    `decision (` in `git log` for the catalogue; consolidated by
    module in `docs/MODULES.md`)
  - **Architectural milestones**:
    - First cross-module `ScoreAdjustment` rail (M24 in 3.4.4)
    - First non-PipelineStage module (M28 in 3.4.8)
    - First Protocol breaking change since Phase 3.3
      (`BacktestStoreProtocol.update_signal_score` in 3.4.8.1)
    - First field promotion from internal-telemetry to domain
      (`opening_closing_score` from M27 in 3.4.7 → 3.4.8)
    - First end-to-end synthetic pipeline test exercising every
      Phase 3.4 stage (3.4.9.3)

### Judgment-call themes (representative selection per sub-phase)

3.4.1 — M21 dealer gamma
  - Provider returns 3 fields not 1 — supports per-strike scoring
  - MAX-of-two-heuristics scoring rather than weighted sum
    (avoided double-counting at institutional threshold)
  - Established the M21-M27 idempotency-on-preset pattern

3.4.2 — M22 event calendar
  - Calendar-day approximation flagged for Phase 3.5 trading-
    calendar overlay
  - `no_event_score = 0.5` not 0.0 (absence of catalyst is
    neutral, not negative)
  - Type-blind scoring (earnings/FDA/FOMC same weight); type-
    weighting deferred until backtest data

3.4.3 — M23 price confirmation
  - Only ThetaData consumer in Phase 3.4 (rest go through UW)
  - Half-day handling deferred to trading-calendar overlay
  - 3-state no-data semantics (`legitimate score=0`,
    `no provider data`, `timeout`) — backtest cost auditing
    relies on these distinctions

3.4.4 — M24 IV exhaustion
  - First `ScoreAdjustment` cross-module rail (no sub-score field
    on EnrichedEvent)
  - Penalty applied to `combined_score_pre`, not `_post`
    (pre-labeler placement matters)
  - Binary penalty (no gradient) — defensible without backtest

3.4.5 — M25 sector peer
  - Two-provider pattern (membership lookup + flow data) —
    pattern reused by M26
  - `no_membership_score = 0.5` distinct from `no_peers_score = 0.0`
  - Direction-blind peer counting (deferred until backtest shows
    direction-matching helps)

3.4.6 — M26 dark pool
  - Boolean flag, not score (gradient over-engineers signal)
  - Symmetric ±30min window (not asymmetric — institutional flow
    can lead OR lag option flow)
  - 100K-share volume floor pre-window-filter

3.4.7 — M27 OI delta
  - 3-state no-data distinction (new_strike / no_data / timeout) —
    Phase 3.5 backtest cost auditing critical
  - `prior_oi == 0` divide-by-zero guard with semantic distinction
  - Score was internal-telemetry; promoted to domain field in
    3.4.8.1 because M28 needed to filter signals

3.4.8 — M28 next-day OI
  - First non-PipelineStage module — `pipeline/validators/`
    package, batch lifecycle
  - `BacktestStoreProtocol.update_signal_score` BREAKING change
    with whitelist (ClassVar frozenset) + bool return + ValueError
    on unknown name
  - Pending state distinct from error (T+1 not yet published →
    retry next batch run, not failure)
  - Validator never raises on individual signal failure (batch
    resilience); failures counted in `ValidationStats.errors`
  - Far-future expiry (2099) in smoke ensures deterministic
    `pending` outcome — exercises wiring not branch coverage

3.4.9 — Closeout
  - `docs/MODULES.md` consolidates ~150 judgment calls per-module,
    not chronologically (operators want all M21 decisions when
    debugging M21)
  - Track B (Phase 3.4.9.2) overrides only M21 + M27 (the
    thesis-relevant modules); other modules inherited unchanged
    pending Phase 3.5 backtest data
  - End-to-end synthetic pipeline test (Phase 3.4.9.3) uses
    NoOp providers — wiring contract, not scoring contract
    (real-data wiring is per-module integration smokes)

### What Phase 3.5 unblocks

Phase 3.4 completes the module surface. Phase 3.5 runs the first
real backtest:

  - Tier-2 historical replay (51 tickers × 24 months) through the
    full 13-stage pipeline + M28 batch validator
  - 4-cell combinatorial comparison (Tier-1+single, Tier-1+fusion,
    Tier-2+single, Tier-2+fusion)
  - Profile threshold tuning based on backtest data (NOT before)
  - Falsification scenarios from Phase 3.2.4 applied — 4 scenarios
    REJECT Formülasyon A; we do not adjust them after seeing
    results

### Deferred to Phase 3.5+ (explicit)

Items flagged during Phase 3.4 but NOT addressed in 3.4.9:

  - **Trading-calendar overlay** — calendar-day vs trading-day
    (M22), half-day handling (M23), prior-session-close holiday
    handling (M27). Currently calendar-day approximations.
  - **TTLCache instrumentation** — providers have caches but
    don't expose hit/miss telemetry. Phase 3.5 metrics dashboard
    will surface these.
  - **`opening_closing_score` SQL column** — currently in JSON
    blob only. Adding dedicated column speeds up M28 batch
    filtering on large runs; Alembic migration cost deferred.
  - **`next_day_oi_confirmed` boolean persistence** — M28 writes
    `m28_confirmation_score` only; derived bool deferred until
    a downstream consumer needs it.
  - **M22 event-type weighting** — earnings/FDA/FOMC currently
    type-blind; backtest may show type-specific edge.
  - **M25 directional peer matching** — currently call/put-blind.
  - **M26 score field** — currently boolean; gradient deferred.
  - **M28 SQL filter via dedicated column** — see opening_closing.
  - **Track B per-module retuning** — only M21 + M27 overridden in
    3.4.9.2; M22-M26 + M28 inherited. Backtest may guide further
    Track B tuning.

### Lokal verification recipe

After receiving the Phase 3.4.9 bundle:

```bash
git bundle list-heads uoa-detector-phase3.4.9.bundle
# expected: <HEAD> refs/heads/phase-3

git fetch uoa-detector-phase3.4.9.bundle phase-3:phase-3
git checkout phase-3
uv sync

uv run pytest -q
# expected: 1391 passed, 20 skipped
uv run mypy --strict src/
# expected: clean across 111 source files
uv run ruff check .
# expected: clean

# Phase 3.4 specific:
uv run pytest tests/unit/test_m2[1-8]_*.py tests/integration/test_phase_3_4_end_to_end.py -v
# expected: ~380 unit tests covering M21-M28 + 3 end-to-end tests

# Live integration smokes (need credentials):
export THETADATA_API_KEY="..."
export UNUSUAL_WHALES_API_KEY="..."
java -jar ThetaTerminal.jar  # in another terminal, for M23 smoke
uv run pytest -m integration -v
# expected: 12 tests, no skips (8 UW + 3 ThetaData + 1 M28)

# M28 batch CLI (dry-run, no credentials needed):
uv run python scripts/run_m28_overnight.py \
  --since-yesterday --in-memory --dry-run
# expected: exit 3 (no runs in empty store) — confirms wiring
```

### Per-commit bisectability sweep

The Phase 3.4 history is bisectable across all 32 sub-commits.
Verifying:

```bash
for commit in $(git log --reverse --format=%H phase-2-complete..phase-3); do
  git checkout -q "$commit"
  uv sync -q
  uv run pytest -q --no-header 2>&1 | tail -1
done
```

Each commit prints "<N> passed, <M> skipped". The total grows
monotonically except 3.4.9.1 (-10 from deleted `test_source_stubs.py`)
and 3.4.8.4 (+1 to skipped count for new M28 smoke).

This document is the closing contract for Phase 3.4. Phase 3.5
will get its own working acceptance doc.
