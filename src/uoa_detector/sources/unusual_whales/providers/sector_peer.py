"""Unusual Whales SectorMap + PeerFlow providers.

Phase 3.3.3.5: Module 25 (Sector Confirmation) consumes two
related Protocols:

  - ``SectorMapProvider`` — ticker → sector, ticker → peers
  - ``PeerFlowProvider``  — recent flow events on peer tickers

UW exposes both surfaces from related endpoints. We implement them
in one file because their fetches share infrastructure (sector
lookup is a prerequisite for peer-set construction in many flows
even though the PeerFlowProvider itself takes ``tickers`` directly).

The two providers are separate classes — they implement separate
Protocols — but they live in the same module for cohesion.

UW endpoint shapes:

  Sector mapping:
    GET /api/stock/{ticker}/info
      → {"data": {"sector": "Technology", "peers": ["MSFT", "GOOGL"]}}

  Peer flow:
    GET /api/option-flow/recent?tickers={comma-joined}&before={iso}
      → {"data": [{"ticker": ..., "side_classification": ...,
                   "executed_at": ..., "label": ...}, ...]}

decision (sector + peers from a single endpoint):
  UW's stock-info endpoint returns both fields. One fetch per
  ticker per TTL window serves both ``sector_of`` and ``peers_of``.
  Cache holds the parsed shape once.

decision (Module 25's PeerFlowProvider takes a tickers seq, not a
single ticker):
  Protocol signature: ``recent_flow(tickers, before, window)``. UW's
  endpoint accepts a comma-joined ticker list, so one fetch per
  (ticker-set, before, window) — but we cache by ticker-set
  serialised as a sorted tuple to keep the key stable across
  caller-side ordering.

decision (cache the FETCH, not the post-filter result):
  Same pattern as dark-pool: fetcher pulls a generous lookback;
  consumer filters in-memory. Two callers asking for slightly
  different windows on the same ticker-set share a single fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.sector_map import PeerFlowEvent
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import timedelta

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


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
        self._cache: TTLCache[dict[str, Any]] = TTLCache(
            ttl_seconds=settings.cache_ttl.sector_map_seconds,
        )

    async def sector_of(self, ticker: str) -> str | None:
        info = await self._cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch(ticker),
        )
        sector = info.get("sector")
        if isinstance(sector, str) and sector:
            return sector
        return None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        info = await self._cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch(ticker),
        )
        peers = info.get("peers", [])
        if not isinstance(peers, list):
            return ()
        # Exclude the ticker itself; uppercase normalised
        out = tuple(
            p.upper()
            for p in peers
            if isinstance(p, str) and p.upper() != ticker.upper()
        )
        return out

    async def _fetch(self, ticker: str) -> dict[str, Any]:
        path = f"/api/stock/{ticker.upper()}/info"
        resp = await self._client.request_json(path)
        data = resp.get("data", {})
        if isinstance(data, dict):
            return data
        return {}


# ---------------------------------------------------------------------------
# PeerFlowProvider implementation
# ---------------------------------------------------------------------------


class UnusualWhalesPeerFlowProvider:
    """``PeerFlowProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        # Use the dark_pool TTL for peer flow because both are
        # high-frequency observation streams. Acceptance doc didn't
        # carve out a separate TTL for peer-flow; if needed later,
        # we add a sibling cache_ttl field.
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.dark_pool_seconds,
        )

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        if not tickers:
            return ()
        # Cache key: sorted tuple of upper-case tickers; one fetch
        # serves any window the consumer requests on this set.
        key = tuple(sorted(t.upper() for t in tickers))
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch(key, before),
        )
        cutoff = before - window
        out: list[PeerFlowEvent] = []
        for row in rows:
            ev = _row_to_peer_flow_event(row)
            if ev is None:
                continue
            if ev.when < cutoff or ev.when > before:
                continue
            out.append(ev)
        return tuple(out)

    async def _fetch(
        self, tickers: tuple[str, ...], before: datetime,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "tickers": ",".join(tickers),
            "before": before.isoformat(),
        }
        path = "/api/option-flow/recent"
        resp = await self._client.request_json(path, params=params)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def _row_to_peer_flow_event(row: dict[str, Any]) -> PeerFlowEvent | None:
    try:
        ticker = str(row["ticker"]).upper()
        when = _parse_iso_utc(str(row["executed_at"]))
        direction_raw = str(
            row.get("side_classification", "neutral"),
        ).lower()
    except (KeyError, ValueError, TypeError):
        return None
    if direction_raw not in ("bullish", "bearish", "neutral"):
        direction_raw = "neutral"
    label_raw = row.get("label")
    label = str(label_raw) if isinstance(label_raw, str) else None
    return PeerFlowEvent(
        ticker=ticker,
        direction=direction_raw,
        when=when,
        label=label,
    )


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
