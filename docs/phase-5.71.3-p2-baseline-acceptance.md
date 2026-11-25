# Phase 5.71.3 — P2 sixty-minute PAPER baseline

Registered 25 November 2026 before implementation. The owner approved
continuing the PAPER roadmap with a reliable first reference comparison.
Keep the existing first-ready enrollment and four entry/ATR arms. Read the
PAPER journal written by the scan observer. The experiment requires no
per-candidate HTTP poll, new market-data request or strategy setting change.

For every newly created stock case other than SPY, store a separate reference
for the original first BUY/READY path. Recovering a missed enrollment records
unknown. Existing cases without this reference stay outside the new cohort.

- Use the first matched stock/SPY pair from a completed regular-session scan
  at least 60 seconds after the first plan was published. Both quote clocks
  must also be at or after that target. Freeze stock, SPY, scan ID, scan
  completion time and recovery status. Use the existing five-minute grace,
  60-second pair skew and original-session close.
- Set exit due 60 minutes after the saved entry observation. Require both
  quote timestamps at or after that target and both no later than scan
  completion. Apply the same grace and original-session close. A late,
  missing, out-of-session or insufficient-notional reference is explicit.
  Recovered outcome scans may use only their already archived quote inputs.
  Keep the case active until both the trade arms and the reference resolve.
- Save transitions in the existing transactional audit and include the
  reference in the per-case export. The dates, fill prices and P&L of the
  existing arms and publication-based markouts retain their original rules.
- Use a fixed $2,500 stock allocation, whole shares and 5, 10 and 20 adverse
  basis points per side. Compare the P2 subset to cash and the expected net of
  an equal-sized uniform sample from the same session's first-ready stock
  cases. Report net excess over SPY for P2 using the paired times. Sum each
  session's paired controls. Count only closed sessions with a full measured
  roster in this comparison. Mixed legacy/eligible rosters, missing prices
  and open sessions are visible in coverage. In a complete session with zero
  P2 cases, P2 and the equal-count control both have zero allocated trades.
- Show coverage, costs, reference state and missingness in the private PAPER
  screen. Expose both quote clocks in the per-case JSON. No outcome fallback
  from daily or minute bars. No strategy selection, broker action or order.

Check old-row compatibility, deadline and half-day handling, recovery,
duplicates, read-only HTML/JSON, paired accounting and the application gate.
