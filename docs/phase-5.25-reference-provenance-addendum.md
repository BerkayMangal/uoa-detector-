# Reference provenance clarification, 2026-11-02

Before deployment: the observed October29 baseline does not by itself prove the
legacy October30 row is missing. The existing adapter anchors to the ATM row's
trade_date, which may still be the previous options session even when the fetch
is today. The native UI rejects the source as not current, but does not expose
that raw date. Treat the initial legacy-gap explanation as a hypothesis, not
established root cause. No claim that a native legacy table was inspected.

Require source-session identity against the explicit live cycle as-of date as
well as exact prior-session identity. A newly fetched prior-session record cannot
become today's price evidence or relative-strength benchmark. Current-session
records may use the receipt-verified absent-day supplement; existing legacy rows
are never replaced. Exit tracking retains its independent fresh-price behavior.
After release, verify either October30 as the correct current-session reference
or an explicit unavailable-source state, without promising a BUY at 14:45.

A source-session mismatch is also rejected by the PAPER spot reader: a fresh
HTTP fetch of an older session is not a new exit observation. Missing entry
baseline alone still does not prevent a genuinely current fresh price from
marking an existing PAPER position. Preserve the old-flow diagnostic alongside
a failed price check, so both independent blockers remain visible and the
existing yesterday-flow test assertion remains intact.

Local verification on the final source: 5,499 hermetic tests passed, 52 key-gated
integration tests skipped, two existing deprecation warnings; strict mypy clean
on 305 source files, ruff clean and lock check consistent. Eleven new regression
cases; focused engine/session suite 108 passed. Native activation and exact
PR/main CI evidence are recorded in the release PR after deployment.
