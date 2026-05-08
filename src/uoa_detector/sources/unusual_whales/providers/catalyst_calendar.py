"""Unusual Whales CatalystCalendarProvider implementation.

Phase 3.3.3.5: implements ``CatalystCalendarProvider`` Protocol
backed by UW's earnings calendar + corporate-events feeds. Returns
the next scheduled catalyst on or after ``after`` for ``ticker``.

The provider is pure data-shipping. Module 22 (Event Calendar)
applies its own scoring (event_score, proximity penalties).

UW endpoint shape (centralised here):

  GET /api/stock/{ticker}/upcoming-events
    → {
        "data": [
          {
            "kind": "earnings",
            "when": "2024-01-25T21:00:00Z",
            "title": "AAPL Q1 FY2024 Earnings"
          },
          ...
        ]
      }

decision (per-ticker cache, in-memory time filtering):
  Same pattern as dark-pool: one fetch per ticker per TTL window,
  consumer filters by 'after' in memory. Calendar entries change
  rarely (TTL=3600s default) so a single fetch serves many lookups
  during a trading day.

decision (UW kind values map directly to CatalystKind Literal):
  UW uses 'earnings', 'fda', 'fomc', 'ma_close', 'guidance',
  'investor_day', 'rebalance' which already match our
  CatalystKind. Anything else falls back to 'other'.

decision (returns None if no events found in or after window):
  ``CatalystCalendarProvider`` Protocol allows None; Module 22
  treats absence as 'no catalyst pressure' and applies a neutral
  score. Operators can swap in a richer source per ticker if
  needed (acceptance doc allows multiple calendar feeds).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from uoa_detector.providers.catalyst_calendar import CatalystEvent, CatalystKind
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


_VALID_KINDS: frozenset[str] = frozenset({
    "earnings", "fda", "fomc", "ma_close",
    "guidance", "investor_day", "rebalance", "other",
})


class UnusualWhalesCatalystCalendarProvider:
    """``CatalystCalendarProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.catalyst_calendar_seconds,
        )

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        """Return the next catalyst for ``ticker`` on or after ``after``."""
        rows = await self._cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch(ticker),
        )
        # Pick the earliest event with when >= after
        candidates: list[CatalystEvent] = []
        for row in rows:
            ev = _row_to_catalyst_event(row, ticker=ticker)
            if ev is None:
                continue
            if ev.when >= after:
                candidates.append(ev)
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
        rows = await self._cache.get_or_fetch(
            ticker.upper(),
            loader=lambda: self._fetch(ticker),
        )
        out: list[CatalystEvent] = []
        for row in rows:
            ev = _row_to_catalyst_event(row, ticker=ticker)
            if ev is None:
                continue
            if window_start <= ev.when <= window_end:
                out.append(ev)
        out.sort(key=lambda e: e.when)
        return tuple(out)

    async def _fetch(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/stock/{ticker.upper()}/upcoming-events"
        resp = await self._client.request_json(path)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def _row_to_catalyst_event(
    row: dict[str, Any], *, ticker: str,
) -> CatalystEvent | None:
    try:
        kind_raw = str(row.get("kind", "other")).lower()
        when = _parse_iso_utc(str(row["when"]))
        title = str(row.get("title", ""))
    except (KeyError, ValueError, TypeError):
        return None
    kind: CatalystKind = cast(
        "CatalystKind",
        kind_raw if kind_raw in _VALID_KINDS else "other",
    )
    return CatalystEvent(
        ticker=ticker.upper(),
        kind=kind,
        when=when,
        title=title,
    )


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
