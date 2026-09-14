"""Unusual Whales CatalystCalendarProvider implementation.

Phase 3.3.3.5: original implementation against
``/api/stock/{ticker}/upcoming-events``.

Phase 3.9.8: that path returns HTTP 404 (verified live 2026-09-14).
Catalysts now come from three live-verified endpoints (contract
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.5). ``ET``
below means ``America/New_York``.

  GET /api/earnings/{ticker}
    -> {"data": [{"source": "estimation" | "company",
                  "report_date": "2026-10-29",
                  "report_time": "premarket" | "postmarket" | "unknown",
                  "ending_fiscal_quarter": "2026-09-30",
                  "street_mean_est": "1.98", "actual_eps": null, ...}]}
    Newest first. Includes the UPCOMING report as source=estimation,
    report_time=unknown.

  GET /api/market/fda-calendar?ticker=<T>&limit=200
    -> {"data": [{"ticker": "GERN", "catalyst": "Top-line Data Due",
                  "drug": "Imetelstat", "status": "Phase 3",
                  "start_date": "2021-04-13", "end_date": null,
                  "target_date": "2025-MID" | "2026-H1" | "" | null
                                 | "YYYY-MM-DD", ...}]}
    The server-side ticker filter is honoured; 200 is the API maximum.

  GET /api/market/economic-calendar
    -> {"data": [{"type": "report" | "fed-speaker" | "fomc",
                  "time": "2026-09-16T18:00:00Z",
                  "event": "U.S. interest rate decision", ...}]}
    Current and next week only; no ticker association.

decision (earnings ``when`` = session boundary on report_date, in ET):
    premarket                          -> 09:30 ET on report_date
    postmarket, unknown, null, other   -> 16:00 ET on report_date
  Matching is case-insensitive. ZoneInfo applies EDT/EST per date
  (09:30 ET = 13:30 UTC EDT / 14:30 UTC EST; 16:00 ET = 20:00 UTC EDT /
  21:00 UTC EST), and ``when`` is stored in UTC. The announcement date
  is kept: ``when.date()`` equals report_date in both regimes, so M22's
  same-day post-event blackout still fires. In M24's
  ``[event_ts - N days, event_ts]`` window, regular-session flow on
  report_date sees a premarket report and never sees a postmarket or
  unknown one (no same-day look-ahead).

decision (earnings dedupe by report_date, known timing preferred):
  One event per report_date. A row with a known timing (premarket or
  postmarket) replaces an earlier row for the same date whose timing is
  unknown; otherwise the first row in response order wins. The title
  records date, timing and source, e.g.
  ``"AAPL earnings 2026-10-29 (unknown, estimation)"``, so an estimated
  date is visible on the decision record.

decision (FDA precise dates only):
    target_date is YYYY-MM-DD                  -> that date
    else start_date == end_date (YYYY-MM-DD)   -> that date
    otherwise (2025-MID, 2026-H1, "", ranges)  -> row dropped
  ``when`` is 16:00 ET of that date (the feed publishes no intraday
  time). Rows whose ``ticker`` is not the requested ticker are dropped,
  as a guard should the server-side filter ever be ignored.

decision (FOMC from the economic calendar, applied to every ticker):
  Only rows with ``type == "fomc"`` become catalysts (kind "fomc",
  ``when`` = parsed ``time``, title = ``event``). Reports and fed
  speakers are not catalysts. The parsed meetings are cached once
  globally and attached to every ticker (frozen 3.4 hypothesis:
  "earnings, FDA, Fed").

decision (per-source isolation):
  ``UnusualWhalesNotFoundError`` (HTTP 404/422) or an empty / non-list
  payload on one source yields no events for that source only. Every
  other ``UnusualWhalesError`` (401/403, 429 or 5xx after retries, open
  breaker) propagates: a bad key must never read as "no catalyst"
  (contract §3.9). A propagated error is not cached; the next call
  fetches again.

decision (caching):
  Per ticker: the parsed, ``when``-sorted ``CatalystEvent`` tuple
  (earnings + FDA + FOMC). Globally: the parsed FOMC meetings, so the
  economic calendar is fetched once across tickers. Both use
  ``cache_ttl.catalyst_calendar_seconds``. ``next_catalyst`` and
  ``catalysts_in_window`` read the same per-ticker tuple. One instance
  is shared by M22 and M24 (``pipeline/stages/live_stages.py``).

D9: the provider never reads the wall clock; callers pass event time.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from uoa_detector.providers.catalyst_calendar import CatalystEvent
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_ET = ZoneInfo("America/New_York")

# US equity regular-session boundaries (NYSE/Nasdaq) in exchange-local
# time. They place a vendor timing label on the clock; they are exchange
# session times, not tunable scoring thresholds (D8).
_SESSION_OPEN_ET = time(9, 30)
_SESSION_CLOSE_ET = time(16, 0)

_EARNINGS_PATH = "/api/earnings/{ticker}"
_FDA_PATH = "/api/market/fda-calendar"
_ECON_PATH = "/api/market/economic-calendar"

# Transport bound: fda-calendar accepts limit 1..200 (vendor default 100).
_FDA_PAGE_LIMIT = 200

_PREMARKET = "premarket"
_KNOWN_REPORT_TIMES: frozenset[str] = frozenset({"premarket", "postmarket"})
_FOMC_TYPE = "fomc"
_UNKNOWN_LABEL = "unknown"
_FDA_FALLBACK_TITLE = "FDA event"
_FOMC_FALLBACK_TITLE = "FOMC"
_GLOBAL_CACHE_KEY = "_global_"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class _FomcMeeting:
    """A parsed FOMC row. Ticker-independent; attached per ticker later."""

    when: datetime
    title: str


class UnusualWhalesCatalystCalendarProvider:
    """``CatalystCalendarProvider`` Protocol implementation backed by UW.

    Aggregates per-ticker earnings, per-ticker FDA calendar rows and the
    global FOMC schedule into one ``when``-sorted tuple per ticker.
    """

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl = settings.cache_ttl.catalyst_calendar_seconds
        self._ticker_cache: TTLCache[tuple[CatalystEvent, ...]] = TTLCache(
            ttl_seconds=ttl,
        )
        self._fomc_cache: TTLCache[tuple[_FomcMeeting, ...]] = TTLCache(
            ttl_seconds=ttl,
        )

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        """Return the next catalyst for ``ticker`` on or after ``after``."""
        for event in await self._events_for(ticker):
            if event.when >= after:
                return event
        return None

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        """Return all catalysts for ``ticker`` with ``when`` in [start, end].

        Phase 3.4.2: feeds M22. Sorted ascending by ``when``.
        """
        events = await self._events_for(ticker)
        return tuple(e for e in events if window_start <= e.when <= window_end)

    async def _events_for(self, ticker: str) -> tuple[CatalystEvent, ...]:
        ticker_u = ticker.upper()
        return await self._ticker_cache.get_or_fetch(
            ticker_u,
            loader=lambda: self._load_ticker(ticker_u),
        )

    async def _load_ticker(self, ticker: str) -> tuple[CatalystEvent, ...]:
        earnings_rows, fda_rows, meetings = await asyncio.gather(
            self._fetch_rows(_EARNINGS_PATH.format(ticker=ticker)),
            self._fetch_rows(
                _FDA_PATH,
                params={"ticker": ticker, "limit": _FDA_PAGE_LIMIT},
            ),
            self._fomc_cache.get_or_fetch(
                _GLOBAL_CACHE_KEY,
                loader=self._load_fomc,
            ),
        )
        events = [
            *_earnings_events(earnings_rows, ticker=ticker),
            *_fda_events(fda_rows, ticker=ticker),
            *(
                CatalystEvent(
                    ticker=ticker, kind="fomc", when=m.when, title=m.title,
                )
                for m in meetings
            ),
        ]
        events.sort(key=lambda e: e.when)
        return tuple(events)

    async def _load_fomc(self) -> tuple[_FomcMeeting, ...]:
        return _fomc_meetings(await self._fetch_rows(_ECON_PATH))

    async def _fetch_rows(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            resp = await self._client.request_json(path, params=params)
        except UnusualWhalesNotFoundError:
            # 404/422: this source has nothing for the input (e.g. no
            # earnings page for an ETF). Empty for THIS source only.
            return []
        return _coerce_data_list(resp)


def _coerce_data_list(resp: object) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


# -- per-source row mappers --------------------------------------------------


def _earnings_events(
    rows: list[dict[str, Any]], *, ticker: str,
) -> list[CatalystEvent]:
    chosen: dict[date, tuple[bool, CatalystEvent]] = {}
    for row in rows:
        day = _parse_iso_date(row.get("report_date"))
        if day is None:
            continue
        report_time = _label(row.get("report_time"))
        known = report_time in _KNOWN_REPORT_TIMES
        previous = chosen.get(day)
        if previous is not None and (previous[0] or not known):
            continue
        clock = _SESSION_OPEN_ET if report_time == _PREMARKET else _SESSION_CLOSE_ET
        source = _label(row.get("source"))
        chosen[day] = (
            known,
            CatalystEvent(
                ticker=ticker,
                kind="earnings",
                when=_et_to_utc(day, clock),
                title=f"{ticker} earnings {day.isoformat()} ({report_time}, {source})",
            ),
        )
    return [event for _, event in chosen.values()]


def _fda_events(
    rows: list[dict[str, Any]], *, ticker: str,
) -> list[CatalystEvent]:
    out: list[CatalystEvent] = []
    for row in rows:
        if _text(row.get("ticker")).upper() != ticker:
            continue
        day = _fda_precise_date(row)
        if day is None:
            continue
        out.append(CatalystEvent(
            ticker=ticker,
            kind="fda",
            when=_et_to_utc(day, _SESSION_CLOSE_ET),
            title=_fda_title(row, ticker=ticker, day=day),
        ))
    return out


def _fda_precise_date(row: dict[str, Any]) -> date | None:
    target = _parse_iso_date(row.get("target_date"))
    if target is not None:
        return target
    start = _parse_iso_date(row.get("start_date"))
    if start is not None and start == _parse_iso_date(row.get("end_date")):
        return start
    return None


def _fda_title(row: dict[str, Any], *, ticker: str, day: date) -> str:
    catalyst = (
        _text(row.get("catalyst"))
        or _text(row.get("status"))
        or _FDA_FALLBACK_TITLE
    )
    drug = _text(row.get("drug"))
    base = f"{ticker} FDA {day.isoformat()}: {catalyst}"
    return f"{base} ({drug})" if drug else base


def _fomc_meetings(rows: list[dict[str, Any]]) -> tuple[_FomcMeeting, ...]:
    out: list[_FomcMeeting] = []
    for row in rows:
        if _text(row.get("type")).lower() != _FOMC_TYPE:
            continue
        when = _parse_timestamp_utc(row.get("time"))
        if when is None:
            continue
        out.append(_FomcMeeting(
            when=when,
            title=_text(row.get("event")) or _FOMC_FALLBACK_TITLE,
        ))
    return tuple(out)


# -- helpers -----------------------------------------------------------------


def _text(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _label(raw: object) -> str:
    return _text(raw).lower() or _UNKNOWN_LABEL


def _parse_iso_date(raw: object) -> date | None:
    text = _text(raw)
    if _ISO_DATE.fullmatch(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_timestamp_utc(raw: object) -> datetime | None:
    text = _text(raw)
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _et_to_utc(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=_ET).astimezone(UTC)
