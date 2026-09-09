"""Unusual Whales REST flow source — Phase 3.7.

A one-shot ``RawFlowSource`` backed by UW's REST ``/api/option-flow/recent``
endpoint (the same endpoint the M25 sector-peer provider uses). Unlike the
WebSocket source, this does not stream: on ``stream()`` it fetches recent
flow for the configured tickers **once**, maps each row through the shared
``map_uw_flow_event`` mapper, yields every mapped print, and completes. That
is the right shape for the once-a-day ``screener`` digest — no reconnect
loop, no ThetaData Terminal, UW API key only.

Why REST and not WS: the documented live WS URL 404s, while the REST
endpoint is what the rest of the system already talks to reliably. A daily
snapshot has no need for a persistent socket.

decision (reuse UnusualWhalesClient, injected):
  The client owns auth (Bearer), rate limiting, retry, and the circuit
  breaker. The REST source takes a client instance (constructor injection),
  so tests pass a fake client with a canned ``{"data": [...]}`` response and
  never touch the network. The client's lifecycle (``aclose``) is owned by
  the caller that constructed it — the source's ``close()`` only flips a
  flag, matching how the CLI owns the store's lifecycle.

decision (one-shot generator, RawFlowSource-compatible):
  ``stream()`` is an async generator that yields all rows then returns, so
  ``SourceFusion`` / ``Pipeline`` consume it exactly like any other
  ``RawFlowSource``. Single-source mode produces ``OptionsPrint``s with
  ``confidence_tier == "single"`` immediately.

decision (optional lookback, no hardcoded default window):
  ``lookback`` is an optional ``timedelta``; when set, rows older than
  ``before - lookback`` are filtered client-side (same pattern as
  sector_peer). Default None means "yield whatever the endpoint returns as
  recent", so there is no magic numeric window baked into code (D8).

decision (``before`` defaults to now-at-fetch):
  This is a live snapshot, not a backtest; ``before`` marks the upper bound
  of "recent up to the moment of the fetch". D9 (event-time, not wall-time)
  governs fusion/decay math on ``event_ts`` inside the pipeline — it is not
  about the REST fetch boundary. A caller can pin ``before`` explicitly for
  a deterministic fetch (tests do).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uoa_detector.sources.unusual_whales.live import map_uw_flow_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from datetime import timedelta

    from uoa_detector.domain.raw_print import RawPrint
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

# The REST endpoint the M25 sector-peer provider already uses. One place to
# change if UW ever moves it.
RECENT_FLOW_PATH = "/api/option-flow/recent"


class UnusualWhalesRestFlowSource:
    """One-shot ``RawFlowSource`` over UW's ``/api/option-flow/recent``.

    Lifecycle::

        src = UnusualWhalesRestFlowSource(
            client=UnusualWhalesClient(api_key=..., settings=...),
            tickers=["AAPL", "MSFT"],
            lookback=timedelta(hours=2),   # optional
        )
        async for rp in src.stream():      # fetches once, yields all
            handle(rp)
        await src.close()                  # flips a flag; does not close client
    """

    source_id: str = "unusual_whales"

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        tickers: Iterable[str],
        lookback: timedelta | None = None,
        before: datetime | None = None,
        source_id: str = "unusual_whales",
    ) -> None:
        self._client = client
        self._tickers = tuple(t.strip().upper() for t in tickers if t.strip())
        self._lookback = lookback
        self._before = before
        self.source_id = source_id
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Fetch recent flow once, yield every mapped ``RawPrint``, complete."""
        if self._closed or not self._tickers:
            return

        before = self._before or datetime.now(tz=UTC)
        params: dict[str, object] = {
            "tickers": ",".join(self._tickers),
            "before": before.isoformat(),
        }
        resp = await self._client.request_json(RECENT_FLOW_PATH, params=params)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return

        cutoff = before - self._lookback if self._lookback is not None else None
        for index, row in enumerate(data):
            if self._closed:
                return
            if not isinstance(row, dict):
                continue
            rp = map_uw_flow_event(
                row,
                source_id=self.source_id,
                source_event_id_fallback=f"uw-rest-fallback-{index}",
            )
            if rp is None:
                continue
            if cutoff is not None and (rp.timestamp < cutoff or rp.timestamp > before):
                continue
            yield rp

    async def close(self) -> None:
        """Mark the source closed. Idempotent.

        Does not close the injected client — the caller that built the
        client owns its lifecycle (``aclose``).
        """
        self._closed = True
