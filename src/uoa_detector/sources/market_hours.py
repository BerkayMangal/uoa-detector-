"""US equity-options regular-trading-hours gate (Phase 4.28).

A pure, dependency-free check used by the live Unusual Whales poll loops to
avoid spending the daily request budget outside market hours — there is no new
options flow to fetch then, so polling overnight/weekends only burns the 15k/day
UW quota and triggers the daily-limit death spiral the screener hit.

Window is expressed in UTC and deliberately covers BOTH US DST states so no
``tzdata`` dependency is needed on the deploy image:

  - 09:30 ET open  = 13:30 UTC (EDT, summer) / 14:30 UTC (EST, winter)
  - 16:00 ET close = 20:00 UTC (EDT, summer) / 21:00 UTC (EST, winter)

Using 13:30–21:00 UTC is the union of both: it always contains the real RTH
session, with at most ~1h of harmless pre/post slack in one DST state (a few
extra polls, not a missed session). Market holidays are NOT modeled — polling
on a holiday wastes at most one morning's budget, which the daily-limit backoff
in the poll loop absorbs.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

_RTH_OPEN_UTC = time(13, 30)
_RTH_CLOSE_UTC = time(21, 0)


def is_market_open(now: datetime) -> bool:
    """True if ``now`` falls within US options RTH (UTC window) on a weekday.

    ``now`` may carry any tzinfo (or be naive-UTC); it is normalised to UTC.
    """
    n = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if n.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return _RTH_OPEN_UTC <= n.timetz().replace(tzinfo=None) <= _RTH_CLOSE_UTC
