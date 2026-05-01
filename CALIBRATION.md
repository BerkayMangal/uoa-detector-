# Calibration surface

Every numeric threshold, weight, multiplier, and penalty in the detector
lives in a `CalibrationProfile` loaded from a YAML file under `profiles/`.
The detector itself contains zero hardcoded calibration values — every
behaviour-affecting number is profile-driven so the same code can run with
spec defaults, ticker-specific overrides, or regime-specific overrides
without a recompile.

This file is the operator's tour of what's tunable and where. It complements
`profiles/v5_default.yaml` (the YAML source of truth) and
`src/uoa_detector/calibration/profile.py` (the typed Pydantic models).

## How to use a custom profile

1. **CLI:** `python -m uoa_detector run --profile path/to/profile.yaml`.
2. **Inheritance:** a child profile sets `inherits_from: v5_default` (or any
   other parent ID) and overrides only the leaves it cares about — deep-merge,
   not shallow. Setting `relative_premium.median_window_days: 60` leaves
   the rest of `relative_premium` (high_ratio, mid_ratio, score bands)
   inherited unchanged. See `profiles/example_ticker_override.yaml`.
3. **Hot-swap:** running pipelines pick up profile changes via
   `CalibrationResolver.reload()`. In-flight events finish on the old
   snapshot; the next event uses the new one. Reload is non-fatal — bad
   YAML logs and the pipeline keeps running on the prior valid state.

## Top-level structure (16 sections)

The default profile is exhaustive — every section below is required and
the loader validates that. A child profile can override any leaf;
unspecified leaves inherit from the parent.

### Identity
- **`profile_id`** — string. Carried in every `SignalDecisionRecord` so
  stored signals can be reconciled against the profile that produced them.
- **`description`** — free-text, surfaced in operator tooling.
- **`inherits_from`** — parent profile ID for deep-merge inheritance, or
  `null` for a root profile.

### `scoring` — combined-score weights (7 leaves)
The seven sub-score weights in the v5 combined-score formula
(uoa, convexity, event, gamma, price_confirmation, sector_confirmation,
time_of_day). Must sum to 1.0 ± 0.01 — validator rejects out-of-band
overrides. Default is the v5 spec breakdown (uoa 0.30, convexity 0.25,
event 0.15, gamma 0.10, price 0.10, sector 0.05, time-of-day 0.05).

### `early_scoring` — pre-UOA-confirmation weights (5 leaves)
Used for early-convexity scoring before a UOA signal has confirmed.
Same sum-to-1.0 invariant. Five components: convexity, cluster_density,
event, gamma, time_of_day. Defaults: convexity 0.40, cluster_density
0.20, event 0.15, gamma 0.15, time_of_day 0.10.

### `penalties` — Module 36 deduction values (8 leaves)
Each is a negative number; the scoring engine adds them post-component-sum.
Eight penalty causes: post_gap_move, post_event, isolated_print,
no_volume_confirmation, no_sector_alignment, weak_iv_movement,
oi_zero_or_unknown, contradicting_price_action. Defaults match the v5
spec values (e.g., post_gap_move = -0.20).

### `penalty_triggers` — what fires each penalty (~12 leaves)
Conditions under which each penalty applies — gap thresholds, time
windows, IV movement bands. Decoupled from `penalties` so an operator
can keep the same magnitudes but change the trigger conditions.

### `contradiction` — Module 36 contradiction-penalty parameters (1+ leaves)
The −0.15 contradiction penalty magnitude plus the trigger conditions
(direction mismatch between flow and price action).

### `dte` — DTE multipliers (5 buckets + LEAP threshold)
Five DTE-bucket multipliers applied to convexity and gamma sub-scores
plus a `leap_threshold` (default 90) above which a print is flagged
LEAP for the labeler's short-circuit. Bucket boundaries and multipliers
match the v5 spec.

### `time_of_day` — Module 39 windows (5 windows + outside-session)
Five named time-of-day windows (open, prime_session, mid_day, power_hour,
close) each with a multiplier. Plus `outside_session_weight` (0.30 default)
and `extended_hours_policy` ('weight' | 'flag' | 'reject', default 'flag').
Windows defined in America/New_York with DST handling.

### `cluster` — Module 38 temporal clustering (9 leaves)
Five density bands (isolated, two_same, three_same, three_escalating,
nearby_strikes), plus `window_minutes`, `decay_minutes`, `escalation_ratio`,
`nearby_strike_count`. Drives both M38 stage scoring and the
ClusterDecayWatcher. Defaults match the v5 spec.

### `sweep` — Module 34 sweep classification (4 leaves)
Three additive bonuses (`iso_bonus`, `cross_venue_bonus`,
`cross_venue_above_ask_bonus`) plus `cross_venue_window_ms` (default 50
— tighter than the 500ms fusion window because sweep classification
requires near-simultaneous multi-venue execution). Bonuses are
non-stacking per Phase 2.6.2 design.

### `relative_premium` — Module 37 banded scoring (6 leaves)
Two ratio thresholds (`high_ratio` 3.0, `mid_ratio` 1.5) and the three
score bands they map to (`score_high` 1.0, `score_mid` 0.5, `score_low`
0.0). Plus `median_window_days` (default 30) — the trailing window
the `MedianTradeSizeProvider` is asked for.

### `label_thresholds` — labeler comparison thresholds (6 leaves)
Every numeric threshold the labeler compares against:
`penalized_below`, `cluster_min`, `cluster_burst`, `relative_premium_uoa`,
`convexity_watch_floor`, `pre_catalyst_event_min`. After Phase 2 these
fully replace the labeler's hardcoded constants.

### `risk_buckets` — label → max-R mapping (per-label R values)
One R-multiple per `SignalLabel` enum value. Drives the risk sizer
without any hardcoded fallback — every label has an explicit entry.

### `fusion` — Phase 2 ingestion-layer tunables (~6 leaves)
Watermark windowing parameters: `window_ms` (500 default — wide enough
for UW's ~500-1000ms latency vs Polygon), `stalled_source_timeout_ms`
(2000), `allowed_lateness_ms` (200). Tier classification:
`unanimous_min_sources` (2), `majority_fraction` (0.5),
`premium_disagreement_tolerance_pct` (0.05).

### `sub_score_missing_behavior` — None-handling per sub-score (~9 leaves)
For each sub-score, an `on_missing` policy (`'raise'` | `'default'`) and
a `default` value. The Phase 2 prompt's "missing data is 0.0, not 0.5"
rule is encoded here as `default: 0.0` for every sub-score that uses
the `'default'` policy. Lets per-profile overrides change a sub-score
from "default to 0.0" to "raise on missing" if a deployment cannot
tolerate gaps.

## Commonly-tuned vs rarely-touched

The loader logs a warning when a child profile loads without overriding
fields the spec marks as commonly tuned. Currently flagged:
`relative_premium.median_window_days`, `cluster.window_minutes`. Both
are slow-moving operator levers (per-ticker baseline lookback, cluster
sensitivity). The list is in `src/uoa_detector/calibration/loader.py`
under `_COMMONLY_TUNED` and is meant to grow as operators identify
levers that benefit from per-deployment tuning.

## What is NOT in profiles

A few things are deliberately not profile-driven:

- **Source connection details** (Polygon URL, IBKR port, UW API token)
  live in `AppSettings` (env-prefixed `UOA_`) and source `*Config`
  objects. These are runtime/deployment config, not detector calibration.
- **Per-stage code paths.** A stub stage's "neutral 0.5 default" for
  Modules 21–27 is the placeholder until Phase 3 wires the real provider;
  changing it via profile would just substitute one neutral default for
  another. Phase 3 fills these in with provider-driven logic that's
  *itself* profile-tunable through the existing knobs (e.g., M22's
  `event_score` will read profile's catalyst-proximity bands once the
  `CatalystCalendarProvider` is real).
