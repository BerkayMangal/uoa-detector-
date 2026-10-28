# Phase 5.45 — the opening decision desk

Frozen before implementation, 2026-10-28 Istanbul. Owner explicitly requests a
useful desk 15–20min before or 15min after US open, reasoned BUY/SELL/WAIT, every
statement recorded and measured later. Deploy authorized. Broker execution is
not authorized. Production PostgreSQL recovery remains an independent blocker:
do not claim real measurement started while durable writes cannot succeed.

## Fixed clock and rules

New opening_desk_v1 overlay, immutable separate profile, prospective dates from
2026-10-28. Use existing declared NYSE calendar, holidays, early closes and ET/DST.
Preparation begins 20min before regular open. First 15min of trading: WAIT on
new entries; risk/exit reviews are never delayed. Entry assessment begins +15min;
these are timing/freshness checks, NOT an invented 15-minute OHLC/VWAP backtest.
Unknown calendar/source stays WAIT. Display actual Istanbul clock times.

Base strategy remains frozen live_alpha_v1. Add structured per-ticker flow and
news observations to its existing saved scan, without altering policy outputs.
Only already committed scan inputs/cached position records are used, no vendor
calls or cap changes. No historical calibration/profile/report/forward_v2 edits.

BUY requires base BUY or bearish option setup READY, matching current RTH day,
source scan created after +15min, scan and underlying price <=300s old, source
price observed in this RTH, current-day flow with last print <=900s old and in
RTH, successful news check (found/none), a valid positive whole-size risk plan
and a ready preferred instrument. Options additionally retain LIVE-leg freshness,
contract quote/cost/risk gates from the original policy. No stock short sale.
Missing critical data, unknown time, future input or any failed check: WAIT.
Stale/new-session views withdraw old BUY; reading a page never generates advice.

SELL/CLOSE is only for a documented existing manual position with a current
fresh observed exit review. PAPER exits are explicitly virtual. Old sticky exit
signals without fresh execution inputs stay REVIEW. Never infer broker holdings
or mark a manual trade closed. A ticker with an active exit review cannot receive
a conflicting new BUY in this projection. Bearish new ideas are PUT/debit-spread
BUY only if ready; otherwise WAIT/avoid, never "sell" unowned stock.

Use source flow/price/SPY/news, risk/quote costs and counter-argument as distinct
explanations with source/availability times. Do not invent success probabilities,
independent-vote scores or call these calibrated alpha. Existing reused-history
studies remain exploratory/rejected; they do not validate this intraday policy.

## Durable journal and measurement

Five new additive alfa_opening_* evidence tables: cycle, decision, primary,
observation, outcome. One atomic, first-writer-winning cycle per profile/scan/
phase, complete advice text, exact plan, scope/instrument, source scan/card hash,
policy/profile/code hashes, source and physical publication times. Every source
scan's decisions (including WAIT and changes of price/stop/reason) are retained,
not merely changes to BUY/WATCH labels. Source full cards remain in immutable
alfa_live_scan; don't duplicate raw vendor history. No overwrite/delete/reset.

Freeze first preparation and first +15min checkpoints in their actual windows:
pre-open -20..open, post +15..+25min. A late/missing checkpoint stays missed; no
backfilled morning plan. Later new names cannot alter its membership. Separately
freeze ONE first actionable entry per ticker/session for prospective assessment;
subsequent updates are a correlated journal, not additional independent trades.
Record actual publication time >= input time and physical clock, never backdate.
No retrospective instructions or use of an existing earlier PAPER fill as the
execution/performance of a later opening recommendation.

Capture only valid as-of current-session cached prices, first observation wins.
Primary-entry reference outcomes: +15/+60min (first observed in next 300s window),
same-day settled close and closes 1/3/5 subsequent declared sessions. Exact dates,
matched SPY timestamps/window, missing prices and pending coverage explicit. No
next-day substitute for a missed intraday target. These are underlying reference
returns, NOT option P&L or proven broker fills. Keep actual PAPER/manual returns
separate. Record source availability and price-basis/cost limitations.

## Product/runtime

Private /acilis: today's clock, concise current BUY/SELL/WAIT, plan/risk, evidence
and counter-argument, frozen checkpoints, all-statement history and primary-entry
outcomes/coverage. Home summary links to it; legacy live_alpha_v1 detail remains
separate. No new public trading-data route. Home opt-in entry notifications may
use a NEW private /api/acilis/uyarilar feed that requires a matching recorded
opening BUY; preserve original feed contract and recorded exit notifications.
Existing client defaults remain compatible. No alert without a committed decision.

New bounded/single-flight observer after existing scan attempt; actual persisted
source only, failure isolated, safe aggregate health status. No HTTP optimizer,
new vendor request or guessed data. Wake the original loop at upcoming prep/open/
+15 anchors using existing cadence as upper bound, preserving quota reservations.
Database failure remains honestly unavailable, never fake in-memory recording.

Verification: causal clock/holiday/DST and boundary tests; critical data/future/
stale/news/option gates; held vs unowned sell and conflict handling; atomic
dedup/concurrency, first checkpoints/entry, immutable changed plans; exact future
reference marks/gaps/SPY pairing and no earlier PAPER-fill leakage; alert privacy,
source commit and page no-write behaviour. Full hermetic pytest, strict mypy,
ruff/lock before every commit. Local actual app desktop/mobile/axe/JS checks with
labelled fixtures; green PR/main CI, matching Railway SHA. State infrastructure
recovery and real observation coverage separately from code deployment.
