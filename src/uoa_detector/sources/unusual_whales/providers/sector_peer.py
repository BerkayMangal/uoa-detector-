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

UW endpoint shapes (Phase 3.3.9.6 — migrated; UW removed the
``peers`` field from /api/stock/{ticker}/info and retired the
multi-ticker /api/option-flow/recent endpoint):

  Sector mapping (two-call pattern):
    GET /api/stock/{ticker}/info
      → {"data": {"sector": "Technology",  -- (peers no longer here)
                   "marketcap": "...", "full_name": "...", ...}}
    GET /api/stock/{sector}/tickers          -- reverse lookup
      → {"data": [{"ticker": "MSFT", ...}, {"ticker": "GOOGL", ...}, ...]}

  Peer flow (per-ticker, N parallel calls for N peers):
    GET /api/stock/{ticker}/flow-recent
      → [{"executed_at": "...", "ticker": "MSFT", "sector": "...",
          "ask_vol": 7722, "bid_vol": 1887, "mid_vol": 1725,
          "option_chain_id": "AAPL260515C00305000",
          "strike": "305", "expiry": "2026-05-15",
          "delta": "...", "open_interest": ..., ...}, ...]
    NB: top-level array, auto-wrapped to {"data": [...]} by the
    client's request_json (Phase 3.3.9.6).

decision (Phase 3.3.9.6 — sector membership requires 2 calls):
  UW's per-ticker info endpoint dropped the ``peers`` array. We
  recover peers by:
    1. ``info(ticker)`` → ``sector`` (single field)
    2. ``stock/{sector}/tickers`` → list of tickers in that sector
  The 24h ``sector_map_seconds`` TTL absorbs the cost — these
  fetches happen at most daily per (ticker, sector) pair.
  ``peers_of(ticker)`` excludes the ticker itself.

decision (Phase 3.3.9.6 — peer flow is N parallel calls):
  UW dropped the bulk-ticker ``/api/option-flow/recent`` endpoint.
  The successor ``/api/stock/{t}/flow-recent`` is per-ticker. For
  the typical M25 peer set (3-5 tickers), we issue the calls in
  parallel via ``asyncio.gather`` and merge results. The TTL
  cache per peer absorbs subsequent calls within the window.

decision (direction derived from ask_vol vs bid_vol):
  Flow rows no longer ship a precomputed ``side_classification``.
  We derive direction from per-row volume split:
    ask_vol > bid_vol → bullish (paying offer)
    ask_vol < bid_vol → bearish (hitting bid)
    ask_vol == bid_vol → neutral
  Rows with neither field treated as neutral. This is the standard
  buy/sell pressure heuristic in market-microstructure literature.

decision (cache the FETCH, not the post-filter result):
  Unchanged from Phase 3.3.3.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.sector_map import PeerFlowEvent
from uoa_detector.sources.unusual_whales.client import UnusualWhalesError
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
    """``SectorMapProvider`` Protocol implementation backed by UW.

    Phase 3.3.9.6: two-call pattern — ``info(ticker)`` for the
    sector field, then ``stock/{sector}/tickers`` for the peer
    list. The legacy ``peers`` field is honoured if present in
    the cached info dict for fixture back-compat.
    """

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl = settings.cache_ttl.sector_map_seconds
        self._info_cache: TTLCache[dict[str, Any]] = TTLCache(ttl_seconds=ttl)
        self._sector_tickers_cache: TTLCache[list[str]] = TTLCache(
            ttl_seconds=ttl,
        )

    async def sector_of(self, ticker: str) -> str | None:
        info = await self._info_cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch_info(ticker),
        )
        sector = info.get("sector")
        if isinstance(sector, str) and sector:
            return sector
        return None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        info = await self._info_cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch_info(ticker),
        )
        # Legacy fixture compat: if the info row carries an explicit
        # peers list (Phase 3.3.3 stubs), prefer it.
        peers_legacy = info.get("peers")
        if isinstance(peers_legacy, list):
            out = tuple(
                p.upper()
                for p in peers_legacy
                if isinstance(p, str) and p.upper() != ticker.upper()
            )
            return out
        # Phase 3.3.9.6 path: derive peers from sector membership.
        sector = info.get("sector")
        if not isinstance(sector, str) or not sector:
            return ()
        tickers = await self._sector_tickers_cache.get_or_fetch(
            sector.upper(),
            loader=lambda: self._fetch_sector_tickers(sector),
        )
        return tuple(t for t in tickers if t != ticker.upper())

    async def _fetch_info(self, ticker: str) -> dict[str, Any]:
        path = f"/api/stock/{ticker.upper()}/info"
        try:
            resp = await self._client.request_json(path)
        except UnusualWhalesError:
            return {}  # 401/429/breaker/network -> no data, never crash
        data = resp.get("data", {})
        if isinstance(data, dict):
            return data
        return {}

    async def _fetch_sector_tickers(self, sector: str) -> list[str]:
        """Phase 3.3.9.6: GET /api/stock/{sector}/tickers → ticker list.

        Response shape: ``{"data": [{"ticker": "MSFT", ...}, ...]}``
        Some sector slugs include spaces in the canonical form (e.g.
        "Health Care"). UW's URL handler accepts the slug with
        URL-encoded spaces; we pass it through ``params``-style
        path interpolation handled by the client.
        """
        path = f"/api/stock/{sector}/tickers"
        try:
            resp = await self._client.request_json(path)
        except Exception:
            return []
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for row in data:
            if isinstance(row, str):
                out.append(row.upper())
            elif isinstance(row, dict):
                t = row.get("ticker")
                if isinstance(t, str) and t:
                    out.append(t.upper())
        return out


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
        # Phase 3.3.9.6: fan-out N parallel per-ticker requests.
        normalised = sorted({t.upper() for t in tickers})
        results = await asyncio.gather(
            *(self._fetch_one(t) for t in normalised),
        )
        merged: list[dict[str, Any]] = []
        for rows in results:
            merged.extend(rows)
        cutoff_after = before - window
        out: list[PeerFlowEvent] = []
        for row in merged:
            ev = _row_to_peer_flow_event(row)
            if ev is None:
                continue
            if ev.when < cutoff_after or ev.when > before:
                continue
            out.append(ev)
        return tuple(out)

    async def _fetch_one(self, ticker: str) -> list[dict[str, Any]]:
        return await self._cache.get_or_fetch(
            ticker,
            loader=lambda: self._fetch_uncached(ticker),
        )

    async def _fetch_uncached(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/stock/{ticker}/flow-recent"
        try:
            resp = await self._client.request_json(path)
        except Exception:
            return []
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def _row_to_peer_flow_event(row: dict[str, Any]) -> PeerFlowEvent | None:
    try:
        ticker = str(row["ticker"]).upper()
        when = _parse_iso_utc(str(row["executed_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    direction = _row_direction(row)
    label = _row_label(row)
    return PeerFlowEvent(
        ticker=ticker,
        direction=direction,
        when=when,
        label=label,
    )


def _row_direction(row: dict[str, Any]) -> str:
    """Phase 3.3.9.6: derive direction from ask_vol vs bid_vol.

    Legacy ``side_classification`` field still honoured for fixture
    back-compat. The ``mid_vol`` portion is ignored — it's
    midpoint trade volume which is direction-neutral by definition.
    """
    legacy = row.get("side_classification")
    if isinstance(legacy, str):
        v = legacy.lower()
        if v in ("bullish", "bearish", "neutral"):
            return v
    ask_vol = _row_int(row, "ask_vol")
    bid_vol = _row_int(row, "bid_vol")
    if ask_vol is None and bid_vol is None:
        return "neutral"
    a = ask_vol or 0
    b = bid_vol or 0
    if a > b:
        return "bullish"
    if a < b:
        return "bearish"
    return "neutral"


def _row_int(row: dict[str, Any], key: str) -> int | None:
    raw = row.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _row_label(row: dict[str, Any]) -> str | None:
    """Phase 3.3.9.6: prefer ``option_chain_id`` (new), fall back to
    legacy ``label`` field.
    """
    label_raw = row.get("label")
    if isinstance(label_raw, str):
        return label_raw
    chain_id = row.get("option_chain_id")
    if isinstance(chain_id, str):
        return chain_id
    return None


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
