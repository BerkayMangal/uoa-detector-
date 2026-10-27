# Phase 5.38 — fixed-candidate stress audit

Frozen before computing these diagnostics, 2026-10-28 Europe/Istanbul. The owner
requests continued research, implementation and deployment. The objective is to
find fragility in the SIX winners already selected in tournament_v1, not select
a new winner after inspecting its final internal test. Old reports, profiles,
acceptance docs, verdicts, prospective arms and live BUY policy remain unchanged.

## Inputs and fixed experiments

ONLY the exact Study F OHLCV source whose hash is in tournament_v1, its six frozen
configurations and its final internal test interval, 2026-07-08..2026-09-18. No
Study G, synthetic prices, new vendors, production secrets, new calls or downloads.
The entire interval was already inspected: these are exploratory diagnostics,
never fresh out-of-sample evidence or multiple-testing-adjusted significance.

For EACH of the six hypotheses perform exactly:

1. Delays of 0/1/2 ADDITIONAL declared exchange sessions before entry, crossed
   with adverse slippage of 5/10/20/50 basis points PER SIDE (12 simulations).
   Retain the original horizon measured from the delayed entry. Freeze original
   signal eligibility/ranking and ATR size input: do not recompute them from
   later bars. No pre-split signal may enter by delaying it into the interval.
   Keep both original signal and deferred signal dates in the ledger. Exits
   remain inside the original split; never shift to the next available bar.
2. One cash-constrained rerun excluding EACH non-SPY name from signal eligibility
   (69 simulations if the validated source has 70 names), at baseline cost/delay.
   Reallocate available slots under the original ranking/cash rules. This is
   explicitly different from simply subtracting a name's realized profits.
3. One rerun excluding signal dates in EACH calendar month touched by the test
   (July/August/September: three simulations). Existing entries/exits in that
   month remain; do not call this exclusion of all P&L earned in that month.

Expected total: 6 × (12 + 69 + 3) = 504 portfolio evaluations, including six
baseline replays. Compare every baseline's metrics, ledger and equity to the
immutable old report. Refuse a report if any mismatch or source hash mismatch.
Every rerun retains the original simulator's $10k cash, whole shares, slot/
per-name caps, no same-day close-funded open, ATR sizing, SPY reference and
missing-data/unresolved-price semantics. No fake stop execution or options P&L.

## Concentration and auditability

On each ORIGINAL baseline ledger group closed P&L and matched excess by ticker
and signal month. Report net after removing the 1 and 3 largest winning trades
as FIXED-LEDGER ACCOUNTING, not a new simulation or confidence interval. Report
gross positive P&L, top winner's fraction of gross gains and baseline ticker
count. Missing P&L/excess stays unknown, not zero. Complete reconciliation is
required; do not collapse unresolved outcomes into a successful empty sample.

Store all 504 configurations/metrics/ledgers/equity in one deterministic gzip
artifact, plus a compact integrity-checked JSON summary with source/profile/
implementation/base-report hashes. Refuse overwriting frozen results. No
optimizer or source-file access during HTTP requests. An offline script is the
only execution path. Validate frozen dependency hashes on report load; loading
failure only hides the supplementary stress section, preserving the live desk.

## Product and decision

Extend the existing private /stratejiler report with a concise stress summary
for the already PAPER-enabled family and collapsible detail for all six. Show
delay/cost metrics, worst excluded name/month, name sensitivity range and
concentration accounting. Label extra entry delay, per-side cost, sample counts,
fewer trades/window truncation, historical reuse, cash reallocation and lack of
open-quote/spread/actual-fill data. Do not imply daily OHLCV simulates liquidity.
No added navigation clutter, automatic orders, changed thresholds or retrospective
promotion. The existing forward experiment remains an experiment irrespective
of the stress results. Public SEO/private auth/noindex/no-store remain intact.

Verification: causal delayed-input/ranking/ATR and split-boundary tests; independent
manual delayed cash accounting; cost/name/month effects and exact legacy replay;
concentration tests with all-loss/missing outcomes; artifact tamper/source rejection;
offline overwrite guard; private-page rendering and fail-isolation. Full hermetic
pytest, strict mypy src/webapp, ruff and uv lock gate before every commit. Local
real-report desktop/mobile browser overflow/accessibility checks. Green PR/main
CI, merge and matching Railway SHA before reporting deployment. Protected
production visual/data inspection needs credentials and must not be fabricated.
