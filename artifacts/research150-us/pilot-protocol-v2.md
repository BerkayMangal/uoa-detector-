# Research150 US pilot — frozen protocol v2

Prepared 2026-11-12 before this pilot's outcome evaluation. This supersedes the
unexecuted initial-scope proposal `pilot-protocol.md`. That earlier document is
retained as an audit record and must not drive this run. US stocks only. The
owner's BIST/VIOP list is idea provenance; original hypotheses remain NOT_TESTED.
No broker execution, live-policy change or known-edge claim is authorized by
this research artifact.

## Frozen universe, dates and axes

- Symbols, fixed order: SPY, QQQ, AAPL, NFLX, NVDA, TSLA, MSFT, META, AMZN.
- Capture complete scheduled US regular sessions from 2026-01-02 through
  2026-11-11, excluding today's partial session. Actual accessible coverage
  governs what can be evaluated; unavailable dates are not backfilled fiction.
- Development/reference dates: January 2–September 30. June–September includes
  a previously examined61-session panel and is explicitly reused history.
- Final period: October1–November11, a **frozen chronological evaluation,
  partly previously observed**, not an untouched holdout. November9–11 outcomes
  were examined earlier in this conversation and other late2026 studies exist.
  Do not use any separately protected/reserved study labels.
- Signal bar widths:1,5,15 minutes. Holding periods:1,5,15 minutes independently.
  Long and short arms are separate. 1h/4h, tick/volume bars, overnight positions,
  options and news-based entry filters are deferred, not implicitly tested.
- Three families × three widths × three holds × two sides =54 candidate
  policies, with54 paired baseline policies. Three cost scenarios give324
  policy-cost views and162 candidate-minus-baseline contrast views. Nothing
  selects the best cost assumption or combines alternative horizons.

The full150-item inventory is maintained by the API inventory agent at
`/workspace/work/uoa-api-investigation/research150/inventory.json`. The execution
registry for these108 policies is `pilot-registry-v2.json` in this directory.

## Exact features and signals

Construct all bars from the same validated native1m OHLCV stream, anchored at
09:30 America/New_York, with the actual exchange calendar, early closes and DST.
Use completed bars only. Feature history resets every regular session. Each
candidate AND baseline requires all nine preceding completed signal bars plus
the current bar, regardless of its family's smaller necessary window. Thus
15m policies first become eligible after ten completed bars, at12:00 ET on an
ordinary session. No session crosses, partial bars, interpolation or forward
fills. Duplicate conflicts and missing constituent minutes make a bar invalid.
Require finite positive OHLC, H>=max(O,C), L<=min(O,C), H>=L, and nonnegative V.

At completed bar k: r[k]=ln(C[k]/O[k]); v[k]=ln(H[k]/L[k])^2/(4*ln(2)). Python
slice notation excludes the right endpoint. Quantiles use linear interpolation,
standard deviations ddof=1, and Pearson correlation uses the stated eight
paired observations. Every prior window excludes k. Missing inputs and zero
variance where a statistic requires variance mean ineligible, not zero signal.
Let s=+1 for long and s=-1 for short.

| Family | Candidate predicate | Baseline predicate |
|---|---|---|
| H138_US_V2 | v[k-1] <= quantile(v[k-9:k-1],0.10), AND long C[k]>max(H[k-4:k]) / short C[k]<min(L[k-4:k]) | The same directional breakout without the squeeze filter |
| H142_US_V2 | z=(r[k]-mean(r[k-8:k]))/sd(r[k-8:k]); long z<=-3 / short z>=3 | Long r[k]<0 / short r[k]>0 |
| H145_US_V2 | s*ln(C[k]/C[k-3])>0 AND s*r[k]>0 AND s*corr(r[k-8:k],ln(1+V[k-8:k]))>0 | The same first two momentum conditions without correlation |

These are new precise US adaptations, not claims to have tested the original
market or its full wording. H142 uses fixed holds, not an inferred60% retracement
target. H145 is a price/volume condition, not an inferred institutional actor.
The maximum lookback is nine completed bars:135 regular-session minutes at15m.
Thresholds and all parameter choices above were fixed without this run's scores.

## Entry, exit, conflicts and units

A signal from a bar ending at t skips the full minute [t,t+1minute). Enter at
the exact native1m bar open at t+1minute; exit at the exact native1m open at
entry+h minutes. These are hypothetical historical trade-price proxies, not
bid/ask executions. This one-minute latency is an assumption, not evidence that
the original system possessed the completed bar then. Store event-time and
physical receipt clocks separately. No price scan forward, stop substitution,
midpoint fill or future best-price selection is allowed.

Both entry and exit must lie strictly before the same regular-session close.
Require all constituent minutes across feature and holding windows; retain
UNKNOWN when data is missing. A missing entry signal is not a no-signal zero.
For each policy/ticker, signals during an open position are ignored. A new entry
at exactly the previous exit is allowed; no overlapping exposure or pyramiding.
Candidate and baseline have independent position state on the same eligibility
roster. Long and short results are never silently netted together.

Per-trade gross return is s*(exit/entry-1). Subtract fixed **round-trip** total
costs of5,10,20bps in three separately labelled scenarios. Five bps is primary;
10 and20 are prespecified stresses. These are assumed aggregate stock trading
frictions, not measured spreads or verified brokerage fees. Do not charge them
again per side. Short results omit verified locate/borrow availability and are
**short-direction price proxies**, never executable short-profit claims.

Use equal1/9 symbol sleeves, with inactive slots cash0. Report the engine's
constant-notional daily proxy explicitly: sum non-overlapping per-trade net
returns divided by9, without redistributing missing/no-trade symbols. If a
compounded daily reference index is displayed, name it a daily-rebalanced
proxy index; it is not verified self-financing execution. Also report additive
net return and daily PnL at a stated fixed reference notional. Do not use
compounding to conceal unverified intraday funding, borrow or fee assumptions.
Every instrument/clock/capital convention change is a new registry variant.

## Availability, coverage and purged walk-forward

Every input retains source hash, endpoint/request parameters, timestamp basis,
first receipt where available, session, interval and coverage reason. Cached
complete=true and HTTP200 do not independently establish complete history.
Audit expected RTH minutes and distinguish verified no-trade intervals from
missing data; do not synthesize either. Freeze data exclusion rules before
looking at returns and report exclusions per symbol/date/width/hold/arm.

Historical OHLCV downloaded later is an **event-time research proxy**. Causal
as-of feature ordering does not prove historical system availability. A
prospective interpretation requires physical availability of every necessary
input by decision time. News uses max(publication, first receipt) with version
and deduplication; current classifications, later OI, final news revisions and
backdated source timestamps cannot enter earlier features. This run has no news
or option predictive condition, so it cannot claim news/option confluence.

For each observation i retain information interval I[i]=[s[i],e[i]], with s[i]
the earliest feature/calibration/universe-selection input and e[i] the latest
label observation/availability, including execution latency and max holding
horizon. Here price features reach135m back and labels reach16m after signal;
actual timestamp intervals, not these maximum shorthand numbers, govern purge.
Any future option lane adds both entry/exit wait windows to e[i].

Expanding development folds use training/reference history starting January2
and ending the previous calendar month; evaluation blocks are the complete
April, May, June, July, August and September session sets. Initial reference
period is January–March. Keep only folds with actual sufficient complete input
history; do not manufacture observations to obtain six folds. The fixed rules
above do not fit thresholds. Any subsequently allowed estimator must be fitted
inside each retained training fold, never across the final evaluation period.

Purge all training observations whose full I[i] intersects ANY evaluation
observation's full interval, including its feature warm-up. Training outcomes
must mature before fitting. Because these features and holds reset each session
and folds split whole sessions, the interval test can correctly purge zero
adjacent-day rows. Still execute and report that check; a generic one-day
embargo or ignoring lookback is not a substitute. If features later cross days,
their actual history extends s[i], and the gap expands automatically. No future
training rows appear after a test block within a fold. A later expanding fold
may reuse earlier evaluation data only after labels mature and under its own
purge, with no adaptive rule changes. Final Oct–Nov results are opened once
after the registry/code/data-manifest hashes are sealed. No retuning follows.

## Paired inference, multiplicity and DSR

The unit of replication is a market session, not a bar, ticker or overlapping
trade. Aggregate all nine symbol sleeves inside each day. Compare each candidate
against its prespecified same-direction baseline using daily paired differences
on an identical data-eligible roster. Legitimate no-trade days are0; missing
outcomes are UNKNOWN. Publish observed-only summaries separately from the full
expected cohort and do not claim missingness is random. Do not allocate missing
symbol capital to successful symbols. If a full daily comparison is not
priceable, retain that missing pair and coverage denominator.

Report net returns at each cost, trade count, distinct sessions, coverage,
drawdown, worst day, turnover, mean delay, symbol/day concentration and paired
baseline difference. Profit factor with no losses is NA, not proof. Fixed
leave-largest-gain-day-out and leave-one-symbol-out diagnostics are sensitivity
checks, never grounds for selecting a better universe.

When there are at least60 complete independent day clusters, primary inference
uses a paired moving-block bootstrap: five-session blocks, ten-session-block
sensitivity,10,000 resamples, seed150. Resample the entire date×symbol×policy
vector together, preserving cross-policy/cross-symbol dependence. Use null-
centered studentized max statistics across all54 primary candidate-minus-baseline
contrasts, with a simultaneous one-sided99% lower confidence bound. Do not
shuffle individual rows. Sixty days is a minimum governance floor, not a power
guarantee; report precision and detectable effect. The October–November period
has fewer than60 sessions, so it cannot receive a confirmation/significance
pass under this protocol regardless of its returns.

The registry includes every original idea and every evaluated variant, failed
arm, inspected ticker subset, horizon, direction, cost view, prompt/model,
perturbation or abandoned search. Distinguish150 proposed ideas from actually
computed/inspected trials. Deterministic identical reruns are not new independent
tests. Previous UOA studies and5184 prior exploratory configurations create a
larger, incompletely known selection history; the new54 candidates do not erase
that history. Costs and diagnostics cannot become uncounted selection choices.

DSR is corroboration only if the search history, return matrix, dependence,
moments and effective sample are defensible. Use unannualized daily excess
returns with a fixed denominator; specify raw kurtosis. The usual PSR term is
Phi((SRhat-SRnull)*sqrt(T-1)/sqrt(1-skew*SRhat+(kurtosis-1)*SRhat^2/4)). For DSR,
SRnull reflects the expected maximum over the actual search, not automatically0.
Analytic expected-maximum formulas assume a known trial distribution/count;
correlated trials and serially dependent returns require justified corrections.
Never set T to trade count or effective trials to three family labels. Never
invent a reduced trial count to create a pass. Clustered max-statistic evidence
is primary; analytic DSR cannot override it or repair prior outcome exposure.

Return DSR=INSUFFICIENT_EVIDENCE if credible trial history/dependence/moments are
missing, sample size is inadequate, or reused data are presented as untouched
confirmation. A pilot-level approximate DSR, if later computed, must disclose
its narrower search universe and cannot constitute a strategy validation pass.

## Options and final dispositions

Option economics require an exact contract selected before its outcome, a causal
chain/snapshot, valid actual contract bid/ask timestamps, bounded complete quote
windows, and actual or explicitly assumed deliverables/fees. Under the existing
separate quote audit: ask+0.01 entry, max(0,bid-0.01) exit, $0.65/side; verified
100-share one-contract net dollars=100*(exit-entry)-1.30. Missing quotes remain
UNKNOWN. Neither delta-scaled stock returns nor stock-cost assumptions validate
options. Native Theta and trade-attached UW NBBO keep distinct provenance and
validator rules. Displayed quotes do not prove fills. Actual Theta access remains
pending the Mac connector; no result from this stock run establishes that access.

Allowed conclusions: BLOCKED_DATA, DESCRIPTIVE_ONLY, INSUFFICIENT_SAMPLE,
REJECTED_AFTER_COSTS, or CANDIDATE_FOR_NEW_FORWARD_PAPER. A fresh confirmation
requires a separately sealed future date range, fixed analysis/thresholds, a
power-derived target of at least60 evaluable days, and no adaptive outcome
inspection. The current chronological split is not that future experiment.
No outcome, overnight effort or request to find something guarantees a winner.
