# Phase 5.50 — causal event calibration workbench

Frozen before implementation or new calibration returns, 2026-10-31. Owner
explicitly authorizes all six proposed investigations, broad measured search,
and existing push/merge/deploy workflow. No broker/order execution, purchase,
live threshold mutation, or promotion of an unvalidated winner.

## Question and constraints

Does ticker/time-of-day-normalized, side-aware observed flow plus subsequent
price acceptance improve the current rule after costs? Separately measure
market/sector/news/gamma additions, holding/entry timing, probability estimates,
and recorded stock/option/spread references. Existing Study F/E, policies and
reports remain frozen. Never read Study G prices. Sparse daily aggregates are
not minute-level flow. No historical quote or first-known timestamp fabrication.
API keys, DB access and current vendor entitlement are external prerequisites;
finish the independent work and report exactly which calculations remain blocked.

## Inputs and causality

An explicit versioned JSON dataset contains observed alert versions/poll coverage,
price observations, original committed live-scan decisions, and quote packets.
Each record carries source time and known/receipt time. Validate timezones,
finite prices/costs, source <= receipt, unique identities and bounds. Canonical
input records never acquire data from a later scan. The read-only SQL exporter
uses existing append-only archives and immutable completed scans; no schema,
HTTP endpoint, worker, vendor cadence or live policy is changed. Refuse truncated
exports and reserved-study paths. Exported source/profile/hash/provenance remain
attached. Private contents never become a public artifact.

Sample the first committed snapshot per ticker/session/15-minute bucket after
opening +15. A failed/incomplete first snapshot stays incomplete. Normalize
15-minute observed directional alert premium against the same bucket on the
previous 20 complete sessions, requiring 10; include genuinely observed zeros.
Poll coverage must span the window, with <=120-second gaps/freshness and no
saturated page. Use existing strict side-aware lab flow parsing; revisions known
after a sample do not alter it. Price acceptance is a positive five-minute
observed underlying change using only points already received. Market/sector
features require explicitly dated as-of observations; unknown stays unknown.
News is recorded checked-found, never presumed positive sentiment. Gamma is an
optional signed input, not an invented live field. Source-only historical API
payloads are not treated as physically observed pre-entry archives.

## Frozen comparison

Profile event_calibration_v1 defines every numerical choice. Baseline is original
recorded LIVE/BUY/READY up advice under its original profile; it is not rebuilt
with a later policy. Six challengers: normalized, normalized_price,
normalized_market, normalized_news, normalized_gamma, normalized_sector.
Ratios 1.5/2/3 x dominance .65/.75 x historical percentile .8/.9 x entry delay
1/5 minutes x hold 15/60/180 minutes: 432 challenger candidates, plus 6 baseline
timing candidates = 438. No grid expansion after returns, no billions claim.
Missing auxiliary evidence prevents that arm. All candidates and counts survive.

Each candidate selects chronologically, one first event/ticker/session, at most
four reference units/day; no result-dependent replacement. It is an independent
reference-unit study, not a cash portfolio or guaranteed fill. Entry is the first
strictly later observed price after the delay; exit is the first observed price
after its horizon, each within five minutes. Publication must precede entry.
No intraday stop/target sequencing inferred from sparse observations. Same-day
boundaries are screened before labels. SPY reference requires those exact source
times; missing observations stay unresolved. Base adverse slippage 5bp/side;
10/20/50bp are stress diagnostics, not another selection chance. No stock
commission/tax assumed. Risk-normalized values require recorded pre-entry stop
risk; planned stock stop risk is not a guaranteed maximum loss.

At least 80 recorded eligible sessions are required: first 60 development,
last >=20 final diagnostic. Expanding folds: first 30/40/50 sessions train, next
10 validate. One full-session embargo and actual label receipt before the next
period are mandatory. Training support >=30 resolved units/10 sessions,
validation support >=10 units/5 sessions, 100% selected-label coverage. Rank
eligible candidates by mean fold daily SPY excess, deterministic key ties; freeze
one winner before final evaluation, never replace it using final results.
Probability/expectancy estimates use only each fold's training outcomes, with
20-observation shrinkage toward the training baseline; final reliability/Brier
scores are out of time. Sparse groups do not get precise success claims.

Final diagnostics preserve whole ledgers, net/reference results, cost and delay
sensitivity, best 1/3 trade removals, ticker/day concentration and descriptive
five-session-block bootstrap intervals (1,000 draws, fixed seed). Report actual
executed trials separately from planned trials. Reused/archive evaluation is
exploratory; no p-value, verified alpha or automatic policy promotion.

## Instruments and external capability

Compare recorded stock, long-call and debit-spread quote references at a common
USD100 planned-risk budget and USD2500 outlay cap, whole units only. Quotes must
be positive, uncrossed, fresh and physically available; same packet for spread
legs, same fixed contracts for later marks, commissions once per side/contract.
UW receipt-dated quotes are quality B, not exchange-timestamped fills. Missing
exit quotes/affordable units/risk remain unknown. No option return from a stock
return, no retrospective IV model as an observed fill. Entry/exit timings and
pair availability are disclosed. Unavailable sector/gamma/quote history is a
capability failure, not a zero feature or a successful ablation.

A bounded read-only UW capability command may probe dated flow/minute-price
routes using existing verified paths, at most 8 requests, verified TLS, no
retry after auth/quota/network failure and no logged secrets. Missing credentials
cause zero network requests. Quota headers/headroom and payload dates are
recorded; reachable/truncated/date-ambiguous data never count as a ready dataset.
Bulk harvesting awaits actual verified capability and headroom; it is not claimed
performed by a probe or by adding an environment requirement.

## Delivery

Read-only inventory with actual local counts and readiness reasons, export and
calibration CLIs, frozen protocol/profile, typed modules and meaningful tests.
No synthetic result is presented as market research. Full hermetic repository
gate before each commit; green PR/main CI and exact production SHA check.
A failed genuine feature release is reported without retry-only commits. Secret
requirements may be saved through onboarding, without values or a claim that a
draft applies/publishes runtime access. No repeated Railway token/log request.
