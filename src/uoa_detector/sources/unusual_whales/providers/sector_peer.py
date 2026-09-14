"""Unusual Whales SectorMap + PeerFlow providers (Module 25).

Phase 3.3.3.5 introduced these providers against guessed endpoints that
never existed (``/api/option-flow/recent`` returned HTTP 404). Phase 3.9.10
moves them onto live-verified endpoints (contract
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.7, probed
2026-09-14). The two classes implement separate Protocols but share this
module for cohesion.

UW endpoints:

  Sector of a ticker:
    GET /api/stock/{ticker}/info
      -> {"data": {"symbol": "AAPL", "sector": "Technology",
                   "issue_type": "Common Stock", "marketcap": "...", ...}}
    ``sector`` is null for an ETF (live: SPY) and empty for an index.

  Peers of a sector, largest first:
    GET /api/screener/stocks?sectors[]=<sector>&order=marketcap
        &order_direction=desc&issue_types[]=Common Stock
      -> {"data": [{"ticker": "NVDA", "sector": "Technology",
                    "marketcap": "5260789000000", ...}, ...]}
    Live Technology order: NVDA, AAPL, MSFT, AVGO, MU, AMD, INTC, ORCL...

  Peer flow:
    GET /api/option-trades/flow-alerts (paginated by ``fetch_flow_alerts``,
    epoch-second cursors, rows newest first). Row keys used here:
    ``ticker``, ``type`` (call|put), ``created_at``, ``total_ask_side_prem``,
    ``total_bid_side_prem``, ``has_multileg``, ``alert_rule``.

decision (peers ranked by market cap, not the sector ticker list):
  Frozen 3.4 M25 takes "the top 5 peer tickers" (``peer_count``), and the
  stage keeps the provider's order. ``/api/stock/{sector}/tickers`` is
  alphabetical over 1749 names, so its first five are arbitrary. The
  screener sorted by market cap gives the sector's largest common stocks
  first. Tickers keep the response order, upper-cased and de-duplicated;
  the queried ticker itself is removed.

decision (sector validated against the documented enum):
  The screener's ``sectors[]`` filter accepts exactly the 11 names in
  ``UW_SECTORS`` (vendored OpenAPI spec). Any other value (null for an ETF,
  empty for an index, an undocumented name) gives no peers and no screener
  call, so a filter UW would ignore can never return an unfiltered list.
  An undocumented name is logged once per provider instance.

decision (caching):
  ``/info`` is cached per ticker and the screener per sector, both on
  ``cache_ttl.sector_map_seconds``. ``sector_of`` and ``peers_of`` share the
  ``/info`` fetch, and one screener fetch serves every ticker in a sector.

decision (peer flow fetched per event-time hour bucket):
  M25 asks for ``[before - window, before]`` once per event. Fetching that
  exact window per event would cost one request per event. Instead the
  fetch covers ``[floor_hour(before) - window, floor_hour(before) + 1h]``
  and is cached by ``(sorted peers, bucket start, window seconds)`` on the
  existing ``cache_ttl.dark_pool_seconds``. Every event in the same UTC hour
  with the same peers and window shares one fetch, and each event's own
  window lies inside its bucket. The bucket is derived from event time, not
  wall time (D9). In live mode a bucket that ends in the future holds only
  the alerts published so far, so the short TTL bounds that staleness.

decision (no look-ahead):
  A bucket holds alerts up to the end of the hour, later than most events
  in it. Each call therefore filters the cached rows to
  ``before - window <= created_at <= before`` (both bounds inclusive).
  ``created_at`` is the alert publication time (live: 5-30 s after the
  last fill), the first moment the alert was knowable.

decision (direction from option type and the aggressor premium split):
  ``total_ask_side_prem`` > ``total_bid_side_prem`` means buyers lifted the
  offer. A bought call and a sold put are bullish; a bought put and a sold
  call are bearish. A multi-leg alert (``has_multileg`` true) is a spread
  whose net direction the split cannot show, so it is neutral. So are a
  tie, an unknown option type, and a missing or unparseable premium.
  Neutral peers drop out of M25's alignment denominator.

decision (errors):
  ``UnusualWhalesNotFoundError`` (HTTP 404/422, an input miss) maps to the
  documented no-data return: ``None`` / ``()``. It is cached like a normal
  empty answer. Every other client error (401/403, 5xx after retries, open
  breaker, rate limit) propagates, as contract §4 keeps for all stages, and
  nothing is cached for it.

caveat (point-in-time membership):
  ``info.sector`` and the screener ranking are current as of the fetch.
  They must not be used in historical replay without a date-aware source
  (contract §4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from uoa_detector.providers.sector_map import PeerFlowEvent
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError
from uoa_detector.sources.unusual_whales.flow_alerts import fetch_flow_alerts
from uoa_detector.sources.unusual_whales.live import _parse_iso_utc
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

__all__ = [
    "SCREENER_STOCKS_PATH",
    "UW_SECTORS",
    "UnusualWhalesPeerFlowProvider",
    "UnusualWhalesSectorMapProvider",
]

_logger = logging.getLogger(__name__)

# Live-verified stock screener endpoint (contract §3.7).
SCREENER_STOCKS_PATH = "/api/screener/stocks"

# The 11 sector names UW documents for ``info.sector`` and accepts in the
# screener's ``sectors[]`` filter (vendored OpenAPI spec, "Sectors").
UW_SECTORS: frozenset[str] = frozenset(
    {
        "Basic Materials",
        "Communication Services",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Financial Services",
        "Healthcare",
        "Industrials",
        "Real Estate",
        "Technology",
        "Utilities",
    },
)

# Screener filter: operating companies only (no ETFs, ADRs or indices).
_PEER_ISSUE_TYPE = "Common Stock"

# Granularity of the peer-flow fetch bucket. A request-batching bound, not a
# scoring threshold (D8): it decides how many events share one fetch, never
# which rows count.
_PEER_FLOW_BUCKET = timedelta(hours=1)

PeerDirection = Literal["bullish", "bearish", "neutral"]


# ---------------------------------------------------------------------------
# SectorMapProvider implementation
# ---------------------------------------------------------------------------


class UnusualWhalesSectorMapProvider:
    """``SectorMapProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl = settings.cache_ttl.sector_map_seconds
        self._info_cache: TTLCache[dict[str, Any]] = TTLCache(ttl_seconds=ttl)
        self._screener_cache: TTLCache[tuple[str, ...]] = TTLCache(
            ttl_seconds=ttl,
        )
        self._warned_sectors: set[str] = set()

    async def sector_of(self, ticker: str) -> str | None:
        """``info.sector`` for ``ticker``; None when empty or unknown."""
        symbol = ticker.strip().upper()
        info = await self._info_cache.get_or_fetch(
            symbol,
            loader=lambda: self._fetch_info(symbol),
        )
        sector = info.get("sector")
        if isinstance(sector, str) and sector:
            return sector
        return None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        """Same-sector common stocks by market cap, largest first, sans self."""
        symbol = ticker.strip().upper()
        sector = await self.sector_of(symbol)
        if sector is None:
            return ()
        if sector not in UW_SECTORS:
            if sector not in self._warned_sectors:
                self._warned_sectors.add(sector)
                _logger.warning(
                    "UW info.sector %r for %s is not a documented sector; "
                    "no peers are fetched for it",
                    sector,
                    symbol,
                )
            return ()
        ranked = await self._screener_cache.get_or_fetch(
            sector,
            loader=lambda: self._fetch_sector_ranking(sector),
        )
        return tuple(peer for peer in ranked if peer != symbol)

    async def _fetch_info(self, symbol: str) -> dict[str, Any]:
        try:
            resp = await self._client.request_json(f"/api/stock/{symbol}/info")
        except UnusualWhalesNotFoundError:
            return {}
        data = resp.get("data")
        if isinstance(data, dict):
            return data
        return {}

    async def _fetch_sector_ranking(self, sector: str) -> tuple[str, ...]:
        params: dict[str, Any] = {
            "sectors[]": sector,
            "order": "marketcap",
            "order_direction": "desc",
            "issue_types[]": _PEER_ISSUE_TYPE,
        }
        try:
            resp = await self._client.request_json(
                SCREENER_STOCKS_PATH, params=params,
            )
        except UnusualWhalesNotFoundError:
            return ()
        data = resp.get("data")
        if not isinstance(data, list):
            return ()
        tickers: list[str] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            raw = row.get("ticker")
            if not isinstance(raw, str):
                continue
            symbol = raw.strip().upper()
            if symbol:
                tickers.append(symbol)
        return tuple(dict.fromkeys(tickers))


# ---------------------------------------------------------------------------
# PeerFlowProvider implementation
# ---------------------------------------------------------------------------


class UnusualWhalesPeerFlowProvider:
    """``PeerFlowProvider`` Protocol implementation backed by UW flow alerts."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        # Peer flow shares the dark-pool TTL: both are high-frequency
        # observation streams and the settings carry no peer-flow TTL.
        self._cache: TTLCache[tuple[dict[str, object], ...]] = TTLCache(
            ttl_seconds=settings.cache_ttl.dark_pool_seconds,
        )

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        """Flow alerts on ``tickers`` with ``before - window <= created_at <= before``.

        :raises TypeError: ``tickers`` is a single string.
        :raises ValueError: ``before`` is naive.
        """
        if isinstance(tickers, str):
            msg = "tickers must be a sequence of symbols, not a single string"
            raise TypeError(msg)
        peers = tuple(sorted({t.strip().upper() for t in tickers if t.strip()}))
        if not peers:
            return ()
        if before.tzinfo is None:
            msg = f"before must be timezone-aware, got {before!r}"
            raise ValueError(msg)
        bucket_start = before.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0,
        )
        key = (peers, bucket_start.isoformat(), window.total_seconds())
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch_bucket(peers, bucket_start, window),
        )
        earliest = before - window
        out: list[PeerFlowEvent] = []
        for row in rows:
            event = _row_to_peer_flow_event(row)
            if event is None:
                continue
            if earliest <= event.when <= before:
                out.append(event)
        return tuple(out)

    async def _fetch_bucket(
        self,
        peers: tuple[str, ...],
        bucket_start: datetime,
        window: timedelta,
    ) -> tuple[dict[str, object], ...]:
        try:
            result = await fetch_flow_alerts(
                self._client,
                tickers=peers,
                older_than=bucket_start + _PEER_FLOW_BUCKET,
                newer_than=bucket_start - window,
            )
        except UnusualWhalesNotFoundError:
            return ()
        if result.non_object_rows:
            _logger.error(
                "Dropping %d non-object UW flow-alert entries for peers %s",
                result.non_object_rows,
                ",".join(peers),
            )
        return result.rows


def _row_to_peer_flow_event(row: Mapping[str, object]) -> PeerFlowEvent | None:
    """Map one flow-alert row; None (logged) when ticker or created_at is bad."""
    ticker_raw = row.get("ticker")
    created_raw = row.get("created_at")
    when: datetime | None = None
    if isinstance(created_raw, str):
        try:
            when = _parse_iso_utc(created_raw)
        except ValueError:
            when = None
    if not isinstance(ticker_raw, str) or not ticker_raw.strip() or when is None:
        _logger.error(
            "Dropping UW peer flow alert without a usable ticker/created_at; "
            "row keys=%s",
            sorted(row.keys()),
        )
        return None
    label_raw = row.get("alert_rule")
    label = label_raw if isinstance(label_raw, str) and label_raw else None
    return PeerFlowEvent(
        ticker=ticker_raw.strip().upper(),
        direction=_row_direction(row),
        when=when,
        label=label,
    )


def _row_direction(row: Mapping[str, object]) -> PeerDirection:
    """Direction implied by option type and the ask/bid premium split."""
    if row.get("has_multileg") is True:
        return "neutral"
    option_type = row.get("type")
    if not isinstance(option_type, str):
        return "neutral"
    option_type = option_type.strip().lower()
    if option_type not in ("call", "put"):
        return "neutral"
    ask_side = _premium(row.get("total_ask_side_prem"))
    bid_side = _premium(row.get("total_bid_side_prem"))
    if ask_side is None or bid_side is None or ask_side == bid_side:
        return "neutral"
    bought = ask_side > bid_side
    if option_type == "call":
        return "bullish" if bought else "bearish"
    return "bearish" if bought else "bullish"


def _premium(raw: object) -> Decimal | None:
    """A finite premium from a JSON string or number; None otherwise."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value
