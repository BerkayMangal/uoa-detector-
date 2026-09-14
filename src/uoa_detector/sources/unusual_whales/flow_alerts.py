"""Unusual Whales flow-alerts paginator — Phase 3.9.4.

``GET /api/option-trades/flow-alerts`` is UW's multi-ticker unusual-flow
feed. Its list rows carry ``id``, the NBBO (``bid``/``ask``), ``iv_end`` and
the aggressor premium split (``total_ask_side_prem``/``total_bid_side_prem``),
live-verified on 2026-09-14 (contract
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.1). One response
holds at most ``FLOW_ALERTS_PAGE_LIMIT`` rows, newest first, so one session
needs several pages.

``fetch_flow_alerts`` is the one place that walks those pages. Its callers are
the REST screener flow source (``rest_flow.py``) and the M25 peer-flow
provider. It does no mapping: rows come back as the raw JSON objects, and each
caller applies its own parser.

decision (epoch-second cursors, widened then clamped):
  ``older_than``/``newer_than`` are sent as integer epoch seconds. The server
  ignores an ISO ``newer_than`` (it substitutes ``older_than`` minus two
  months), so ISO is never sent. Seconds are coarser than ``created_at``
  (microseconds). The server window is therefore widened to whole seconds:
  ``older_than`` is rounded up and ``newer_than`` is rounded down. The exact
  bounds are then applied client-side to ``created_at``. Widening only
  re-sends boundary rows, which the dedupe removes. Truncating ``older_than``
  down would silently skip rows created inside the boundary second.

decision (page walk and stop rules):
  Page N+1 is requested with ``older_than`` = the oldest ``created_at`` on
  page N. The boundary is inclusive (live: exactly one overlapping row), so
  rows are deduplicated by ``id``. A row without ``id`` is keyed on
  ``option_chain`` + ``created_at`` + ``total_size``. The walk stops when:
    - the page is shorter than the limit (window exhausted);
    - the oldest row on the page is before the cutoff (cutoff reached);
    - a full page adds no new rows, has no parseable ``created_at`` to use as
      a cursor, or would not move the cursor back in time.
  The last group is the truncation guard. It ends a walk that cannot advance,
  logs a WARNING, and sets ``FlowAlertsResult.truncated``, because older rows
  inside the window may exist and were not reached.

decision (cutoff):
  With ``newer_than`` the cutoff is ``newer_than``. Without it the cutoff is
  00:00 America/New_York on the ET date of the newest ``created_at`` on the
  first page: the latest session that has alerts. An open-ended walk would
  otherwise page back through two months of alerts.

decision (what is returned):
  Rows with ``created_at`` inside ``[cutoff, older_than]``, in response order
  (newest first, page by page). A row whose ``created_at`` is missing or does
  not parse is passed through, so the caller's mapper drops it with an ERROR
  that names its keys. A schema change is never silent. Array entries that
  are not JSON objects are not returned. They are counted in
  ``non_object_rows`` so a caller can report them as dropped.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from uoa_detector.sources.unusual_whales.live import _parse_iso_utc

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

__all__ = [
    "FLOW_ALERTS_PAGE_LIMIT",
    "FLOW_ALERTS_PATH",
    "FlowAlertsResult",
    "FlowAlertsStopReason",
    "fetch_flow_alerts",
]

_logger = logging.getLogger(__name__)

# Live-verified multi-ticker flow-alerts endpoint (contract §3.1).
FLOW_ALERTS_PATH = "/api/option-trades/flow-alerts"

# Rows per page: the API maximum for this endpoint. A transport bound, not a
# scoring threshold (D8).
FLOW_ALERTS_PAGE_LIMIT = 200

# Sessions are dated in US Eastern time (contract §3: "ET").
_ET = ZoneInfo("America/New_York")

FlowAlertsStopReason = Literal[
    "short_page",
    "cutoff_reached",
    "no_new_rows",
    "no_cursor",
    "cursor_not_advancing",
]

# Stop reasons that end the walk before the window was exhausted.
_TRUNCATING_STOPS: frozenset[str] = frozenset(
    {"no_new_rows", "no_cursor", "cursor_not_advancing"},
)


@dataclass(frozen=True)
class FlowAlertsResult:
    """Outcome of one paginated flow-alerts fetch.

    :ivar rows: raw alert objects whose ``created_at`` lies inside
        ``[cutoff, older_than]``, plus rows whose ``created_at`` did not
        parse. Deduplicated, newest first.
    :ivar pages: HTTP requests made.
    :ivar cutoff: lower time bound applied: ``newer_than``, or 00:00 ET of
        the latest session. None when the first page had no parseable
        ``created_at``.
    :ivar stop_reason: why the walk ended.
    :ivar non_object_rows: array entries that were not JSON objects.
    """

    rows: tuple[dict[str, object], ...]
    pages: int
    cutoff: datetime | None
    stop_reason: FlowAlertsStopReason
    non_object_rows: int = 0

    @property
    def truncated(self) -> bool:
        """True when the truncation guard ended the walk early."""
        return self.stop_reason in _TRUNCATING_STOPS


async def fetch_flow_alerts(
    client: UnusualWhalesClient,
    *,
    tickers: Sequence[str],
    older_than: datetime,
    newer_than: datetime | None = None,
) -> FlowAlertsResult:
    """Fetch every flow alert for ``tickers`` inside a time window.

    :param client: the shared UW client (auth, rate limit, retry, breaker).
        Its errors propagate unchanged.
    :param tickers: underlying symbols. They are stripped, upper-cased,
        de-duplicated and comma-joined into ``ticker_symbol``. At least one
        is required: without ``ticker_symbol`` the endpoint returns
        market-wide flow.
    :param older_than: inclusive upper bound on ``created_at`` (tz-aware).
    :param newer_than: inclusive lower bound on ``created_at`` (tz-aware).
        None selects the latest session: 00:00 ET of the newest alert's ET
        date.
    :raises TypeError: ``tickers`` is a single string.
    :raises ValueError: no ticker, or a naive datetime bound.
    """
    if isinstance(tickers, str):
        msg = "tickers must be a sequence of symbols, not a single string"
        raise TypeError(msg)
    symbols = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    if not symbols:
        msg = (
            "fetch_flow_alerts needs at least one ticker; an empty "
            "ticker_symbol would request market-wide flow"
        )
        raise ValueError(msg)
    for name, bound in (("older_than", older_than), ("newer_than", newer_than)):
        if bound is not None and bound.tzinfo is None:
            msg = f"{name} must be timezone-aware, got {bound!r}"
            raise ValueError(msg)

    base_params: dict[str, object] = {
        "ticker_symbol": ",".join(symbols),
        "limit": FLOW_ALERTS_PAGE_LIMIT,
    }
    if newer_than is not None:
        base_params["newer_than"] = math.floor(newer_than.timestamp())
    cursor = math.ceil(older_than.timestamp())
    cutoff = newer_than

    seen: set[str] = set()
    collected: list[tuple[dict[str, object], datetime | None]] = []
    non_object_rows = 0
    pages = 0
    stop_reason: FlowAlertsStopReason

    while True:
        response = await client.request_json(
            FLOW_ALERTS_PATH, params={**base_params, "older_than": cursor},
        )
        pages += 1
        data = response.get("data")
        page: list[object] = data if isinstance(data, list) else []

        oldest: datetime | None = None
        newest: datetime | None = None
        new_rows = 0
        for item in page:
            if not isinstance(item, dict):
                non_object_rows += 1
                continue
            created = _created_at(item)
            if created is not None:
                if oldest is None or created < oldest:
                    oldest = created
                if created <= older_than and (newest is None or created > newest):
                    newest = created
            key = _dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            new_rows += 1
            collected.append((item, created))

        if pages == 1 and cutoff is None and newest is not None:
            cutoff = _session_start(newest)

        if len(page) < FLOW_ALERTS_PAGE_LIMIT:
            stop_reason = "short_page"
            break
        if new_rows == 0:
            stop_reason = "no_new_rows"
            break
        if oldest is None:
            stop_reason = "no_cursor"
            break
        if cutoff is not None and oldest < cutoff:
            stop_reason = "cutoff_reached"
            break
        next_cursor = math.ceil(oldest.timestamp())
        if next_cursor >= cursor:
            stop_reason = "cursor_not_advancing"
            break
        cursor = next_cursor

    if stop_reason in _TRUNCATING_STOPS:
        _logger.warning(
            "UW flow-alerts pagination stopped early (%s) after %d page(s) "
            "for %s at older_than=%d; older alerts inside the window may be "
            "missing",
            stop_reason,
            pages,
            base_params["ticker_symbol"],
            cursor,
        )

    rows = tuple(
        row
        for row, created in collected
        if created is None
        or (created <= older_than and (cutoff is None or created >= cutoff))
    )
    return FlowAlertsResult(
        rows=rows,
        pages=pages,
        cutoff=cutoff,
        stop_reason=stop_reason,
        non_object_rows=non_object_rows,
    )


def _created_at(row: dict[str, object]) -> datetime | None:
    """Parsed ``created_at`` of a row, or None if missing or unparseable."""
    raw = row.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return _parse_iso_utc(raw)
    except ValueError:
        return None


def _dedupe_key(row: dict[str, object]) -> str:
    """Identity of a flow-alert row: its ``id``, else chain + time + size."""
    row_id = row.get("id")
    if row_id is not None and row_id != "":
        return f"id:{row_id}"
    return (
        f"alt:{row.get('option_chain')}|{row.get('created_at')}"
        f"|{row.get('total_size')}"
    )


def _session_start(moment: datetime) -> datetime:
    """00:00 America/New_York on the ET date of ``moment``, as UTC."""
    et_date = moment.astimezone(_ET).date()
    return datetime.combine(et_date, time(0), tzinfo=_ET).astimezone(UTC)
