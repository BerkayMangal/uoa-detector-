"""Unusual Whales CatalystCalendarProvider implementation.

Phase 3.3.3.5: original implementation against the now-deprecated
``/api/stock/{ticker}/upcoming-events`` endpoint.

Phase 3.3.9.4: UW retired the per-ticker upcoming-events endpoint.
Catalyst data is now spread across three endpoints that we
aggregate in this provider:

  GET /api/earnings/{ticker}
    → {"data": [{"source": "...", "report_date": "2026-07-30",
                  "report_time": "after-hours" | "pre-market" |
                  "at-close" | "unknown", "expected_move": ...,
                  "street_mean_est": "1.87", ...}, ...]}

  GET /api/market/fda-calendar (global; we filter by ticker)
    → {"data": [{"ticker": "GERN", "event_type": "Top-line Data Due",
                  "start_date": "2021-04-13",
                  "target_date": "2025-MID" | "2025-Q3" | YYYY-MM-DD,
                  "description": "...", "has_options": true, ...}, ...]}

  GET /api/market/economic-calendar (global; no ticker association)
    → {"data": [{"type": "report", "time": "2026-05-15T13:15:00Z",
                  "event": "Capacity utilization",
                  "reported_period": "April",
                  "forecast": "...", "prev": "..."}, ...]}

decision (Phase 3.3.9.4 — 3 endpoints aggregated, DTO unchanged):
  Phase 3.4 M22 stage and the CatalystEvent DTO are frozen. The
  provider issues the three GETs in parallel via asyncio.gather,
  maps each source's rows into CatalystEvent instances, and
  unions them in a single in-memory list. Per-source caches use
  the same ``catalyst_calendar_seconds`` TTL.

decision (kind mapping):
    earnings/{t}              → kind = "earnings"
    fda-calendar (filtered)   → kind = "fda"
    economic-calendar         → kind = "fomc" if "fed" in event
                                  else "other"
  M22 only differentiates between "any catalyst within window" and
  "no catalyst"; the kind label is for telemetry. The "fed"
  heuristic is intentionally loose — any econ row mentioning the
  Federal Reserve gets the FOMC label so M22's blackout window
  works correctly around Fed meetings.

decision (earnings ``when`` derivation):
    report_time="pre-market"  → 13:30 UTC (08:30 ET DST pre-open)
    report_time="after-hours" → 21:00 UTC (16:00 ET DST close)
    report_time="at-close"    → 21:00 UTC
    report_time other/unknown → 21:00 UTC (post-close default)
  Non-DST is off by 1h but M22 windows are day-granular so the
  drift is irrelevant within the pre-event window.

decision (FDA ``when`` derivation, with text-target fallback):
    target_date matches YYYY-MM-DD → use it at 12:00 UTC midday
    target_date is text like "2025-Q3" / "2025-MID" → skip the row
                  (we can't anchor a precise calendar day)
    target_date missing            → fall back to start_date 12:00 UTC
  Rows without any parseable date are dropped silently.

decision (economic-calendar global → applies to every ticker):
  Macro events like FOMC affect every ticker we score. We
  intentionally do NOT ticker-filter the economic-calendar feed.
  M22 then sees every macro event for every ticker the operator
  cares about.

decision (per-source cache keys):
  earnings: ticker.upper()  — per-ticker
  fda:      "_global_"      — global feed, filtered per-call
  econ:     "_global_"      — global feed
  Two providers' caches share TTL but use distinct in-memory
  TTLCache instances so the in-flight-lock dedup works per source.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from datetime import date as _date
from datetime import time as _time
from typing import TYPE_CHECKING, Any, cast

from uoa_detector.providers.catalyst_calendar import CatalystEvent, CatalystKind
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_GLOBAL_CACHE_KEY = "_global_"


class UnusualWhalesCatalystCalendarProvider:
    """``CatalystCalendarProvider`` Protocol implementation backed by UW.

    Phase 3.3.9.4: aggregates three sources — per-ticker earnings,
    global FDA calendar (ticker-filtered), and global economic
    calendar (passed through for every ticker).
    """

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl = settings.cache_ttl.catalyst_calendar_seconds
        self._earnings_cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=ttl,
        )
        self._fda_cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=ttl,
        )
        self._econ_cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=ttl,
        )

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        """Return the next catalyst for ``ticker`` on or after ``after``."""
        events = await self._all_events_for_ticker(ticker)
        candidates = [e for e in events if e.when >= after]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.when)
        return candidates[0]

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        """Return all catalysts for ``ticker`` with ``when`` in [start, end].

        Phase 3.4.2: feeds M22. Sorted ascending by ``when``.
        """
        events = await self._all_events_for_ticker(ticker)
        out = [e for e in events if window_start <= e.when <= window_end]
        out.sort(key=lambda e: e.when)
        return tuple(out)

    async def _all_events_for_ticker(
        self, ticker: str,
    ) -> list[CatalystEvent]:
        """Fetch + aggregate events from all three UW sources in parallel."""
        ticker_u = ticker.upper()
        earnings_rows, fda_rows, econ_rows = await asyncio.gather(
            self._earnings_cache.get_or_fetch(
                ticker_u,
                loader=lambda: self._fetch_earnings(ticker_u),
            ),
            self._fda_cache.get_or_fetch(
                _GLOBAL_CACHE_KEY,
                loader=self._fetch_fda,
            ),
            self._econ_cache.get_or_fetch(
                _GLOBAL_CACHE_KEY,
                loader=self._fetch_econ,
            ),
        )
        out: list[CatalystEvent] = []
        for row in earnings_rows:
            ev = _earnings_row_to_event(row, ticker=ticker_u)
            if ev is not None:
                out.append(ev)
        for row in fda_rows:
            if str(row.get("ticker", "")).upper() != ticker_u:
                continue
            ev = _fda_row_to_event(row, ticker=ticker_u)
            if ev is not None:
                out.append(ev)
        for row in econ_rows:
            ev = _econ_row_to_event(row, ticker=ticker_u)
            if ev is not None:
                out.append(ev)
        return out

    async def _fetch_earnings(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/earnings/{ticker}"
        resp = await self._client.request_json(path)
        return _coerce_data_list(resp)

    async def _fetch_fda(self) -> list[dict[str, Any]]:
        path = "/api/market/fda-calendar"
        resp = await self._client.request_json(path)
        return _coerce_data_list(resp)

    async def _fetch_econ(self) -> list[dict[str, Any]]:
        path = "/api/market/economic-calendar"
        resp = await self._client.request_json(path)
        return _coerce_data_list(resp)

def _coerce_data_list(resp: object) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data", [])
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


# -- per-source row mappers --------------------------------------------------


def _earnings_row_to_event(
    row: dict[str, Any], *, ticker: str,
) -> CatalystEvent | None:
    report_date_raw = row.get("report_date")
    if not isinstance(report_date_raw, str):
        return None
    try:
        day = _date.fromisoformat(report_date_raw)
    except ValueError:
        return None
    report_time = str(row.get("report_time", "unknown")).lower()
    when = datetime.combine(day, _time_for_earnings(report_time), tzinfo=UTC)
    return CatalystEvent(
        ticker=ticker,
        kind="earnings",
        when=when,
        title=f"{ticker} earnings ({report_time})",
    )


def _time_for_earnings(report_time: str) -> _time:
    if report_time in ("pre-market", "pre-open", "before-open"):
        return _time(hour=13, minute=30)  # 08:30 ET DST
    # default + after-hours + at-close → 21:00 UTC (16:00 ET DST close)
    return _time(hour=21, minute=0)


def _fda_row_to_event(
    row: dict[str, Any], *, ticker: str,
) -> CatalystEvent | None:
    target_raw = row.get("target_date")
    start_raw = row.get("start_date")
    when_date = _parse_yyyy_mm_dd(target_raw) or _parse_yyyy_mm_dd(start_raw)
    if when_date is None:
        return None
    title = str(row.get("event_type") or row.get("description") or "FDA event")
    when = datetime.combine(when_date, _time(hour=12, minute=0), tzinfo=UTC)
    return CatalystEvent(
        ticker=ticker,
        kind="fda",
        when=when,
        title=title,
    )


def _econ_row_to_event(
    row: dict[str, Any], *, ticker: str,
) -> CatalystEvent | None:
    time_raw = row.get("time")
    if not isinstance(time_raw, str):
        return None
    try:
        when = _parse_iso_utc(time_raw)
    except ValueError:
        return None
    event_name = str(row.get("event", "economic event"))
    kind_raw = "fomc" if "fed" in event_name.lower() else "other"
    return CatalystEvent(
        ticker=ticker,
        kind=cast("CatalystKind", kind_raw),
        when=when,
        title=event_name,
    )


# -- helpers -----------------------------------------------------------------


def _parse_yyyy_mm_dd(raw: object) -> _date | None:
    if not isinstance(raw, str):
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
