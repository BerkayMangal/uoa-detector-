"""Unusual Whales REST flow source — Phase 3.7, endpoint corrected in 3.9.4.

A one-shot ``RawFlowSource`` over UW's REST
``GET /api/option-trades/flow-alerts``. Unlike the WebSocket source it does not
stream. On ``stream()`` it fetches the alert window for the configured tickers
**once**, walking every page with ``fetch_flow_alerts``. It maps each row
through the shared ``map_uw_flow_event`` mapper, yields the prints in
event-time order, and completes. That is the right shape for the once-a-day
``screener`` digest: no reconnect loop, no ThetaData Terminal, UW API key only.

Phase 3.7 targeted ``/api/option-flow/recent``, which never existed (HTTP 404,
verified 2026-09-14). Contract
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.1 moves the source
to flow-alerts.

decision (reuse UnusualWhalesClient, injected):
  The client owns auth (Bearer), rate limiting, retry, and the circuit
  breaker. The REST source takes a client instance (constructor injection),
  so tests pass a fake client with canned ``{"data": [...]}`` pages and
  never touch the network. The client's lifecycle (``aclose``) is owned by
  the caller that constructed it — the source's ``close()`` only flips a
  flag, matching how the CLI owns the store's lifecycle.

decision (one-shot generator, RawFlowSource-compatible):
  ``stream()`` is an async generator that yields all rows then returns, so
  ``SourceFusion`` / ``Pipeline`` consume it exactly like any other
  ``RawFlowSource``. Single-source mode produces ``OptionsPrint``s with
  ``confidence_tier == "single"`` immediately.

decision (window, no hardcoded default length):
  With ``lookback`` the window is ``[before - lookback, before]``. It is sent
  to the server as epoch ``newer_than``/``older_than`` and clamped
  client-side. Without ``lookback`` the window is the latest session with
  alerts: 00:00 ET of the newest alert's ET date, up to ``before``. This
  supersedes the Phase 3.7 default ("whatever one response returns"), which
  silently covered about three hours of one session. No numeric window
  length is baked into code (D8); the session boundary is a calendar fact.

decision (``before`` defaults to now-at-fetch):
  This is a live snapshot, not a backtest; ``before`` marks the upper bound
  of "recent up to the moment of the fetch". D9 (event-time, not wall-time)
  governs fusion/decay math on ``event_ts`` inside the pipeline — it is not
  about the REST fetch boundary. A caller can pin ``before`` explicitly for
  a deterministic fetch (tests do).

decision (event-time order):
  flow-alerts pages are newest first. Prints are yielded in ascending
  ``(timestamp, source_event_id)`` order, the order the WS feed and backtest
  replay produce. Event-time stages (M38 temporal cluster, cluster decay)
  score each print against the prints already seen, so newest-first input
  would let a print see later flow (D9).

decision (prints fusion cannot take are dropped loudly, never fabricated):
  The mapper requires bid/ask. IV and OI are optional on ``RawPrint``, but
  the single-source fusion path raises ``DataSourceError`` on a print with no
  IV or no OI, which aborts the whole run. Such a print is dropped, counted
  in ``rows_dropped``, key-sampled, and logged at ERROR with the row's keys.
  A value is never invented.

decision (diagnostics):
  ``rows_fetched`` counts the rows the paginator returned inside the window,
  plus non-object array entries. ``rows_mapped`` counts prints that passed
  the mapper and the IV/OI check, including prints later windowed out by an
  ``executed_at`` outside the window. ``rows_dropped`` counts non-object
  entries, unmappable rows and IV-/OI-less prints. ``pages_fetched``,
  ``cutoff`` and ``truncated`` expose the pagination outcome.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from uoa_detector.sources.unusual_whales.flow_alerts import (
    FLOW_ALERTS_PATH,
    fetch_flow_alerts,
)
from uoa_detector.sources.unusual_whales.live import map_uw_flow_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from datetime import timedelta

    from uoa_detector.domain.raw_print import RawPrint
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

# FLOW_ALERTS_PATH is re-exported: callers and tests that route on the flow
# endpoint import it from here.
__all__ = ["FLOW_ALERTS_PATH", "UnusualWhalesRestFlowSource"]

_logger = logging.getLogger(__name__)

# How many dropped-row key-sets to retain for the diagnostic (Phase 3.8). A
# rendering cap for the stderr line, not a scoring threshold — a handful of
# samples names a schema mismatch without flooding the log.
_MAX_DROPPED_SAMPLES = 5


class UnusualWhalesRestFlowSource:
    """One-shot ``RawFlowSource`` over UW's ``/api/option-trades/flow-alerts``.

    Lifecycle::

        src = UnusualWhalesRestFlowSource(
            client=UnusualWhalesClient(api_key=..., settings=...),
            tickers=["AAPL", "MSFT"],
            lookback=timedelta(hours=2),   # optional; default latest session
        )
        async for rp in src.stream():      # fetches all pages once, yields all
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
        # Phase 3.8 self-diagnostic counters (read after stream() drains).
        self.rows_fetched = 0
        self.rows_mapped = 0
        self.rows_dropped = 0
        self.dropped_key_samples: list[tuple[str, ...]] = []
        # Phase 3.9.4 pagination outcome (read after stream() drains).
        self.pages_fetched = 0
        self.cutoff: datetime | None = None
        self.truncated = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Fetch the alert window once, yield mapped prints in event-time order."""
        if self._closed or not self._tickers:
            return

        before = self._before or datetime.now(tz=UTC)
        window_start = before - self._lookback if self._lookback is not None else None
        result = await fetch_flow_alerts(
            self._client,
            tickers=self._tickers,
            older_than=before,
            newer_than=window_start,
        )
        self.pages_fetched = result.pages
        self.cutoff = result.cutoff
        self.truncated = result.truncated
        self.rows_fetched = len(result.rows) + result.non_object_rows
        self.rows_dropped += result.non_object_rows

        prints: list[RawPrint] = []
        for index, row in enumerate(result.rows):
            if self._closed:
                return
            rp = map_uw_flow_event(
                row,
                source_id=self.source_id,
                source_event_id_fallback=f"uw-rest-fallback-{index}",
            )
            if rp is None:
                # Unmappable row (missing/unparseable required field). The
                # mapper already logged it; record its KEYS (not values).
                self._record_drop(row)
                continue
            missing = [
                name
                for name, value in (
                    ("implied_volatility", rp.implied_volatility),
                    ("open_interest", rp.open_interest),
                )
                if value is None
            ]
            if missing:
                _logger.error(
                    "Dropping UW flow print without %s (source_id=%s): fusion "
                    "requires it and values are never fabricated; row keys=%s",
                    ", ".join(missing),
                    self.source_id,
                    sorted(row.keys()),
                )
                self._record_drop(row)
                continue
            self.rows_mapped += 1
            if window_start is not None and (
                rp.timestamp < window_start or rp.timestamp > before
            ):
                continue
            prints.append(rp)

        prints.sort(key=lambda p: (p.timestamp, p.source_event_id))
        for rp in prints:
            if self._closed:
                return
            yield rp

    async def close(self) -> None:
        """Mark the source closed. Idempotent.

        Does not close the injected client — the caller that built the
        client owns its lifecycle (``aclose``).
        """
        self._closed = True

    def _record_drop(self, row: dict[str, object]) -> None:
        """Count a dropped row and keep a bounded sample of its keys."""
        self.rows_dropped += 1
        if len(self.dropped_key_samples) < _MAX_DROPPED_SAMPLES:
            self.dropped_key_samples.append(tuple(sorted(row.keys())))
