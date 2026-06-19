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

_logger = logging.getLogger(__name__)
_BASE = "https://api.unusualwhales.com"
_BENCHMARK = "SPY"


def latest_close(ticker: str) -> float | None:
    key = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        return None
    try:
        resp = httpx.get(
            f"{_BASE}/api/stock/{ticker.upper()}/ohlc/1d",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            return float(data[-1]["close"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        _logger.warning("price fetch failed for %s: %s", ticker, exc)
    return None


def snapshot(ticker: str) -> tuple[float | None, float | None]:
    """Return ``(underlying_close, spy_close)`` — either may be None."""
    return latest_close(ticker), latest_close(_BENCHMARK)
