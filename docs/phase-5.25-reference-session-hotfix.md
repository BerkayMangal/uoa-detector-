# Live Alpha — exact previous-session reference, 2026-11-02

Registered before correction. Native 14:15 UTC QQQ/SPY/TSLA cards use 2026-10-29
as previous close on 2026-11-02, while the latest completed exchange session is
2026-10-30. Verified research daily coverage includes October30, but the legacy
live daily-bar table is incomplete. Do not present a multi-session change as a
current-session change or derive a new entry from that stale anchor. Existing
owner authorization covers diagnosis/fixes/tests/merge/deploy. No order action.

Add a SELECT-only current-context adapter that fills only an absent exact prior
session from the existing immutable calibration bars, requiring matching physical
hash receipts available by the cycle time, settled source timestamps and valid
regular OHLC. Never overwrite a legacy row, manufacture historical availability,
query a vendor, rewrite records or reuse labels for calibration. Contradictory,
invalid, future or missing receipts stay unavailable. Attribution must mention
both storage paths accurately.

Price context must fail closed unless its previous close is exactly the calendar's
previous exchange session. A failed benchmark cannot provide relative strength.
Unknown calendar remains unknown. Preserve fresh-spot PAPER exit handling: closing
an existing virtual position needs a fresh observed price, not a complete entry
baseline. Live thresholds/profiles, ATR formula, timing, 15-minute gate, original
recommendation records and frozen research artifacts stay unchanged.

Test Monday/holiday boundaries, stale/missing/invalid source, physical receipts,
source conflict/no overwrite, failed benchmark isolation and fresh-price exits.
Run full unchanged gate and green PR/main CI; verify actual deployed cards show
October30 or an explicit unavailable state. No new brokerage/real trade advice,
backtest, automatic repair write, request id or API budget change.

The original job fixture's thirty weekdays started August10 and stopped September18,
four sessions before its September24 decision. Shift that synthetic window to
August13–September23 so the existing sourced-BUY scenarios still have a valid
prior close. Preserve every original test assertion and value/ATR expectation;
this repairs fixture timing rather than exempting old dates from the new guard.
