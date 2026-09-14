"""Price snapshots for the trade journal's market-neutral metric.

Fetches the latest daily close for a ticker (and SPY, the benchmark) from the
UW REST API. Synchronous + best-effort: returns ``None`` on any failure or
when no UW key is present, so the journal still logs trades (and option P&L)
without prices — only the market-neutral excess is left uncomputed.

Latest close is a deliberate, honest proxy for the trade-time price: most
screener trades are held across days, so close-to-close returns are the clean
market-neutral comparison. Intraday precision is not the point here.
"""

from __future__ import annotations

import logging
import os

import httpx

from webapp.ohlc import regular_session_closes

_logger = logging.getLogger(__name__)
_BASE = "https://api.unusualwhales.com"
_BENCHMARK = "SPY"


def latest_close(ticker: str) -> float | None:
    """Newest regular-session (``market_time == "r"``) close, by ``date``.

    The ohlc/1d payload is newest first and mixes pre/regular/post rows, so
    rows are filtered and ordered explicitly (``webapp.ohlc``). During RTH the
    current day's "r" row is the session in progress, so the value can be an
    intraday price. None when the newest regular close is not numeric.
    """
    key = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        return None
    try:
        # 3s cap: this runs on the request thread (journal POST); a slow/blocked
        # UW must not hang the submit or starve the threadpool.
        resp = httpx.get(
            f"{_BASE}/api/stock/{ticker.upper()}/ohlc/1d",
            headers={"Authorization": f"Bearer {key}"},
            timeout=3.0,
        )
        resp.raise_for_status()
        body = resp.json()
        bars = regular_session_closes(body.get("data") if isinstance(body, dict) else None)
        if bars:
            return bars[-1].close
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        _logger.warning("price fetch failed for %s: %s", ticker, exc)
    return None


def snapshot(ticker: str) -> tuple[float | None, float | None]:
    """Return ``(underlying_close, spy_close)`` — either may be None.

    Best-effort: a missing key or slow feed yields (None, None) within ~3s each,
    so a journal entry always saves (option P&L is manual); only the
    market-neutral metric is left uncomputed.
    """
    return latest_close(ticker), latest_close(_BENCHMARK)
