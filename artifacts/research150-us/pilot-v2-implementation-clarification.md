# Pre-outcome implementation clarification, v2

2026-11-12. Applies to protocol SHA256
456dfa9b66a3de453c1d3af46722c590c81adf30fc35738cfbcd992e3ca2ac29.
This does not change a signal, threshold, universe, side, cost or execution rule.
No pilot outcome was calculated or inspected to prepare this clarification.

1. Nine preceding signal bars span135 minutes at15m **before the current bar
   starts**. The full input interval through the current signal bar's end spans
   ten bars,150 minutes. Always set feature_start=start_time[k-9], as the actual
   timestamp rule in v2 requires. The current bar's OHLCV is an input too.
   H142/H145 baselines retain the same relevant nonzero-variance eligibility as
   their candidates. H138 does not require nonzero range variance; its quantile
   remains defined for a flat prior range.

2. Inference uses daily candidate-minus-baseline contrasts, not trade returns
   treated as independent. For primary block length b=5, define centered daily
   contrast x_t-mean(x) and autocovariance gamma_l=sum of matching lagged centered
   products divided by T. Use Bartlett HAC mean standard error

       SE = sqrt((gamma_0 + 2*sum[(1-l/b)*gamma_l, l=1..b-1])/T).

   For b=10 sensitivity, use lags1..9. A nonpositive/nonfinite variance or a
   practically unstable sparse series gives NA, not an artificial denominator
   floor or an infinite significance statistic.

3. Sample overlapping consecutive blocks of b sessions with replacement using
   starts0..T-b, concatenate and truncate to T; use the same sampled dates for
   the entire54-column contrast vector. Start from each column's null-centered
   daily observations. For each of10,000 draws, take the maximum across columns
   of resampled mean divided by that column's **original-sample HAC SE**.
   This is a fixed-scale standardized max bootstrap; do not describe it as
   re-estimating studentization within each draw. The99th percentile q gives
   simultaneous one-sided lower bounds mean_j-q*SE_j. Seed150. Prespecified
   cost stresses are reported separately and do not replace the5bps primary.

4. Require at least60 complete paired sessions. Resampling must not make two
   dates that straddle an excluded/missing session artificially consecutive;
   sample blocks only from contiguous available-session runs in the official
   calendar. Report eligible block count and any fragmentation; insufficient
   block support gives INSUFFICIENT_EVIDENCE. The Oct–Nov chronological period
   stays below the minimum and therefore receives descriptive summaries only.

5. A positive paired difference establishes, at most, relative improvement
   against the specified baseline. It does not establish positive absolute
   returns: a less-negative strategy can beat a losing baseline. A cost-negative
   candidate cannot pass an economic gate. No new tradeable-edge or fresh-
   confirmation claim follows from the paired statistic, especially on reused
   history and assumed stock costs. Any additional absolute-profitability
   significance test must be separately registered and corrected for selection.

6. Canonical download metadata naming a bar's end_time as bar_available_at is
   an event-time proxy only. Physical availability remains source.received_at;
   any collector metadata amendment must preserve that distinction. The engine
   must not infer contemporary possession from a historical bar timestamp.
