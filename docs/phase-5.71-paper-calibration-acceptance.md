# Phase 5.71 — Forward PAPER calibration

Frozen 23 November 2026 before implementation. The owner requested an
autonomous assessment of entry confirmation and stop sizing, and a
repeatable PAPER journal.

## Cohort and arms

Observe the first eligible Live Alpha BUY / READY stock card per ticker and
US regular session after activation. Freeze its plan, source clock, source
decision, source profile identity and the prospective experiment configuration.
Enrollment uses a completed live scan with current-session flow. Previously
closed positions are archival evidence and cannot become forward cases.

The fixed arms compare the same first AL plan with and without a current
same-scan BUY / READY stock requirement. Both use the first plan's unchanged
1.5 ATR stop. Two additional confirmed arms use 2 and 2.5 times the original
ATR. All arms use the same original entry price bounds, original plan reference
and absolute target. An eligible entry quote must be observed after the plan
is published. A denied fill may wait for a later ready scan in that session.
Stock shares are whole and limited to the original $100 planned risk ceiling
and $2,500 maximum modeled notional at the plan reference. The same 5 bp
per side stock slippage applies. Filled risk relative to the fixed stop can
differ from reference risk. No broker path is involved.

## Observation and reporting

Use the source scan's cached stock observations. When an open case is absent
from the scan, an already stored ATM receipt may supply a quote only if known
by the source scan's input clock. Fresh sampling in the verified regular
session is required. Exit on observed stock stop or fixed target, qualifying
current opposite flow, or expiry at the fifth following session. A signaled
exit with missing price waits for a valid price from that same session and
becomes unknown at the next session if none was seen. A pending entry expires
after its original session. Daily OHLC is not substituted into forward fills.
Observed adverse and favorable excursions are extrema of sampled prices.

At +15 and +60 minutes from publication, paired stock and SPY observations
must be at or after the horizon, within a five-minute window and within 60
seconds of each other. A reference pair must have been present at signal
publication. Store bids/asks as unknown because cached stock marks do not
provide them. Subtract the SPY move in percentage points before trading costs.

Keep the paper ledger transactional, versioned, restart-safe and private.
Record each processed scan for active cases with exact source clocks,
entry decision, quote/missingness, opposing-flow evidence and per-arm action.
Expose the reasons, states, risk, modeled fills, sampled excursions,
benchmark coverage and per-case JSON on an authenticated read-only route.
Unknown outcomes remain separate. Matched differences use fixed $100 budget
units and include unfilled arms as cash only where both arms have resolved
results. The monitor does not choose winners or modify live recommendations.

Keep the scheduler, existing PAPER and option measurement paths independent.
Offline analysis must preserve time and cohort boundaries, record execution
assumptions and quantify price and flow coverage before comparing policies.
A prospective cohort can start producing outcomes only during future market
sessions. Any strategy release decision requires separate evidence.
