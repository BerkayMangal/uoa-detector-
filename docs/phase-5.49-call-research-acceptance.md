# Phase 5.49 — call evidence and relative-strength research

Frozen before implementation/results, 2026-10-31. Owner explicitly approves the
call scorecard, relative-strength/retest research, instrument-cost explanation,
coverage improvements and measured backtests, with existing push/merge/release
authorization. No broker connection, order execution or live-policy tuning.

## Evidence and limits

Read-only inventory: Study F contains 17,640 rows, 70 names, 252 dates,
2025-09-18 through 2026-09-18; SHA256
ed1f6d8871e900f959793edb8ed7da4b52a1502da3c7974e2279e74e6a7205ca.
SPY/QQQ exist, NFLX and sector ETFs do not. Initial QA found zero invalid OHLC
rows and zero absolute close-to-close moves above 50%; adjustment quality and
survivorship are not thereby established. Study E has only sparse aggregate
flow, no historical executable option quotes. The local seed has no live call
records. No production DB/API/auth credentials are present. Do not manufacture
NFLX attribution, intraday/VWAP behavior, option P&L or forward samples.

Study F is already searched: ALL new historical windows/results/intervals are
EXPLORATORY. Never read reserved Study G prices, change earlier reports/verdicts,
or label a recycled split independent confirmation. Historical-price membership
is fixed from the archived source; exclude SPY/QQQ/DIA/IWM/GLD/TLT as references
only, leaving 64 stocks. Record missing NFLX and sector history in the report.

## Frozen daily proxy experiment

New immutable profile call_research_v1 holds every numeric decision. Four arms:
baseline, persistence, delay_control, retest. Baseline at signal close requires
SPY daily return <0, own return >0 and own-minus-SPY return >=0.5 percentage
points (the existing P2 magnitude). Persistence additionally requires own daily
excess over SPY >0 on each of the last three consecutive sessions and three-
session cumulative own return greater than QQQ's. Entry next session open.
Delay_control uses the baseline signal but enters one session later. Retest
uses that SAME baseline signal, requires next session low <= signal close and
next session close >= signal close, then enters the following session open.
No close confirmation with a fictitious earlier fill or intraday order inference.

Every arm is a separate cash-only $10,000 portfolio, whole shares, at most
$2,500 outlay/name and four active names, no leverage/overlapping re-entry.
Rank simultaneous entries by signal-day SPY excess descending, then ticker.
Entries precede that day's closing exits; no same-day reuse of closing cash.
Hold exactly 3 sessions including entry (primary); 1/5 are diagnostics only.
Run 5/10/20/50bp adverse slippage PER SIDE, no extra commission/tax assumed.
No daily-bar stop, options model or "best" variant selection. Reject signals
whose exact exit lies outside its split; missing exact prices remain unresolved
and capital is not released. SPY/QQQ comparisons use the same entry/exit dates,
allocated outlay and cost assumptions, never full-capital vs partial-capital.

Fixed reused windows: development 2025-09-18..2026-03-19, review
2026-03-30..2026-06-18, audit 2026-06-30..2026-09-18. Earlier data is warmup only;
no pre-split signal may enter later. Four arms x three horizons x four costs x
three windows = 144 disclosed replays, no optimizer. Save every ledger/equity
path, missing/boundary counts, data/profile/code hashes and the fixed trial
count. Report primary cost sensitivity, per-window consistency, concentration
and ledger removal of best 1/3 wins, without retuning. A 1,000-draw fixed-seed
10-session-block bootstrap of daily matched excess gives descriptive 95%
intervals only, not a fresh significance test or validated-alpha probability.

## Calls, coverage and future evidence

Private scorecard reads immutable opening decisions, their exact source scan
and hash-verified recorded instrument costs. Display original reasons, plan,+publication/source timestamps, validity and missing evidence. Link each update
to its canonical first-AL record; only that exact record owns existing measured
reference outcomes. Never assign earlier PAPER fills or first-entry returns to
a later update. Underlying/SPY references remain separate from actual PAPER
option results. No recommendation re-computation or HTTP writes/vendor calls.

Daily capture targets include the archived 70-name universe plus NFLX and all
11 sector ETFs (82 unique names), deduplicated with existing basket/board/hot
names. Reuse only the existing scheduled ohlc/1d job, its error/cap semantics
and append-only capture. This adds bounded daily requests, not a new polling
loop, paid tier, uncapped harvester or modified historical universe. Expose
per-name current/missing/invalid/warmup evidence and source/membership limits.
Do not claim production gaps are filled without actual observations.

Separate shadow cohorts start with signals on/after 2026-11-02. Freeze all four
arms' first daily rosters/inputs/profile before next entry open; no late fill or
membership rewrite. Existing cached as-of references/context only; missing
context stays unknown. Use exact subsequent-session prices with actual observed
availability. Future unit records are not the historical cash portfolio and
are never a live BUY/PAPER promotion. NFLX can join prospective membership only
under its explicit new membership version, never fabricated historical bars.

## Delivery

Freeze profile before any hypothesis returns. Preserve all frozen strategy/
research packages, original reports, live thresholds, append-only evidence and
public/auth boundaries. New research code hashes explicitly enumerate research
files so adding presentation adapters does not rewrite a frozen experiment.
Test prefix causality, exact calendars/gaps/boundaries, retest timing, budget/
cash/whole shares/costs, matched references, reproducibility, source rejection,
scorecard provenance/no-write/privacy and forward first-writer/as-of behavior.
Actually calculate the replays and integrity-check ledgers; synthetic cases are
software tests only. Full hermetic pytest/mypy/ruff/lock before every commit,
green PR/main CI, actual browser checks and real production SHA verification.
Report an access/deployment blocker plainly; do not ask for the same log/token
or make blind retry commits. A profitable proxy alone never becomes proven alpha.
