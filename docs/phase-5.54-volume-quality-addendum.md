# Source volume quality — before return calculation

The read-only source inventory found16 negative regular-minute volume fields
among95,160 RTH OHLC rows; all OHLC geometry/timestamps remain valid. Their
vendor correction semantics are unverified. Four mixed-date daily packets remain
unopened. This phase uses OHLC-only references and never computes VWAP, volume
acceptance or liquidity/executable fills from these values. Preserve anomaly
counts/affected ticker-dates, disclose overlap in reference paths where feasible,
and do not delete a row after seeing its P&L. No new optimized volume filter or
additional candidate arm is introduced. Any later volume-based experiment needs
an independently verified source/correction policy and a new registration.
