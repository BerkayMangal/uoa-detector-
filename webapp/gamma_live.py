"""Live dealer-gamma map from Unusual Whales (Phase 4.19).

The full-tier UW key exposes ``/api/stock/{t}/greek-exposure/strike`` (per-strike
dealer GEX) and ``/api/stock/{t}/iv-rank`` (spot close + IV + 1y IV rank). That
removes the ThetaData-Terminal dependency for the gamma board: a background loop
refreshes the regime / flip / walls / IV-rank per watchlist ticker straight into
the ``gamma_regime`` table the webapp reads — live, on Railway.

Two calls per ticker, rate-limited by the shared client (90/min cap), refreshed
every few minutes, so the per-minute UW burst budget is never threatened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.sources.market_hours import is_market_open
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesClient,
    UnusualWhalesError,
)
from webapp.gamma import GammaRepo

_logger = logging.getLogger(__name__)
_DEFAULT_PROFILE = Path("profiles/v5_default.yaml")
_REFRESH_BACKOFF_S = 60.0
# Outside RTH the gamma board is static and UW would only burn the daily
# request budget — idle this long between market-closed checks (Phase 4.28).
_CLOSED_MARKET_SLEEP_S = 300.0


def _f(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _flip(net_by_strike: list[tuple[float, float]], spot: float) -> float | None:
    """Cumulative-net-GEX zero crossing nearest the spot (ascending strikes)."""
    net_by_strike.sort(key=lambda x: x[0])
    cum = 0.0
    prev_strike, prev_cum = None, 0.0
    crossings: list[float] = []
    for strike, net in net_by_strike:
        cum += net
        if prev_strike is not None and (prev_cum <= 0 < cum or prev_cum >= 0 > cum):
            span = cum - prev_cum
            x = prev_strike if span == 0 else prev_strike - prev_cum * (strike - prev_strike) / span
            crossings.append(x)
        prev_strike, prev_cum = strike, cum
    return min(crossings, key=lambda s: abs(s - spot)) if crossings else None


def context_from_uw(
    rows: list[dict[str, object]], iv: dict[str, object],
) -> dict[str, float | None] | None:
    """Build the gamma-map fields from greek-exposure rows + iv-rank payload."""
    spot = _f(iv.get("close"))
    if spot is None or spot <= 0:
        return None
    lo, hi = 0.6 * spot, 1.4 * spot  # ignore far-OTM/ITM noise strikes
    net_total = 0.0
    net_by_strike: list[tuple[float, float]] = []
    best_call: tuple[float, float] | None = None  # (gex, strike)
    best_put: tuple[float, float] | None = None
    for r in rows:
        strike = _f(r.get("strike"))
        call_gex = _f(r.get("call_gex")) or 0.0
        put_gex = _f(r.get("put_gex")) or 0.0
        if strike is None:
            continue
        net_total += call_gex + put_gex  # regime uses the whole book
        if not (lo <= strike <= hi):
            continue  # flip + walls use the near-spot band only
        net_by_strike.append((strike, call_gex + put_gex))
        if best_call is None or call_gex > best_call[0]:
            best_call = (call_gex, strike)
        if best_put is None or abs(put_gex) > abs(best_put[0]):
            best_put = (put_gex, strike)
    if not net_by_strike:
        return None
    iv_rank = _f(iv.get("iv_rank_1y"))
    flip = _flip(net_by_strike, spot)
    return {
        "spot": spot,
        "net_gex": net_total,
        "flip": round(flip, 2) if flip is not None else None,
        "call_wall": best_call[1] if best_call else None,
        "put_wall": best_put[1] if best_put else None,
        "atm_iv": _f(iv.get("volatility")),
        "iv_pct": (iv_rank / 100.0) if iv_rank is not None else None,
    }


def _next_earnings_date(resp: object, *, today: str) -> str | None:
    """Soonest report_date >= today from a UW /api/earnings payload; None if
    absent/garbage."""
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        return None
    future = sorted(
        str(r["report_date"]) for r in data
        if isinstance(r, dict) and isinstance(r.get("report_date"), str)
        and str(r["report_date"]) >= today
    )
    return future[0] if future else None


async def fetch_one(client: UnusualWhalesClient, ticker: str) -> tuple[str, dict[str, float | None]] | None:
    try:
        gex = await client.request_json(f"/api/stock/{ticker.upper()}/greek-exposure/strike")
        ivr = await client.request_json(f"/api/stock/{ticker.upper()}/iv-rank")
    except UnusualWhalesError as exc:
        _logger.warning("gamma refresh failed for %s: %s", ticker, exc)
        return None
    rows = gex.get("data") if isinstance(gex, dict) else None
    iv_data = ivr.get("data") if isinstance(ivr, dict) else None
    iv0 = iv_data[0] if isinstance(iv_data, list) and iv_data else iv_data
    if not isinstance(rows, list) or not isinstance(iv0, dict):
        return None
    ctx = context_from_uw(rows, iv0)
    if ctx is None:
        return None
    today = datetime.now(UTC).date().isoformat()
    try:
        e_resp = await client.request_json(f"/api/earnings/{ticker.upper()}")
        ctx["next_earnings"] = _next_earnings_date(e_resp, today=today)  # type: ignore[assignment]
    except UnusualWhalesError as exc:
        _logger.warning("earnings fetch failed for %s: %s", ticker, exc)
        ctx["next_earnings"] = None
    return str(iv0.get("date", "")), ctx


async def refresh_all(client: UnusualWhalesClient, tickers: list[str], repo: GammaRepo) -> int:
    done = 0
    for ticker in tickers:
        result = await fetch_one(client, ticker)
        if result is not None:
            as_of, ctx = result
            repo.upsert(ticker, as_of, ctx)
            done += 1
    return done


async def gamma_refresh_loop(
    *,
    tickers: list[str],
    database_url: str,
    interval_s: float = 240.0,
    profile_path: Path = _DEFAULT_PROFILE,
) -> None:
    """Refresh the gamma map for every watchlist ticker, every ``interval_s``.

    Resilient: a transient error backs off and retries; cancel (shutdown) exits.
    """
    profile = load_profile(profile_path)
    repo = GammaRepo(database_url)
    # Drop + recreate gamma_regime on startup so a schema change (the Phase 4.34
    # next_earnings column) is picked up WITHOUT a manual migration — same
    # full-rebuild pattern as scripts/gamma_snapshot.py. create_all alone does
    # NOT add a column to an existing table, so without this the first upsert
    # against the old prod table would fail and stall the gamma refresh. The
    # table is a full live rebuild each cycle, so dropping it costs only the few
    # seconds until the first refresh_all repopulates it.
    repo.reset()
    _logger.info("gamma refresh loop started for %s", tickers)
    while True:
        try:
            if not is_market_open(datetime.now(UTC)):
                await asyncio.sleep(_CLOSED_MARKET_SLEEP_S)
                continue
            client = UnusualWhalesClient(
                api_key=Credentials().unusual_whales_api_key,
                settings=profile.data_sources.unusual_whales,
            )
            try:
                n = await refresh_all(client, tickers, repo)
                _logger.info("gamma map refreshed: %d/%d tickers", n, len(tickers))
            finally:
                await client.aclose()
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            _logger.info("gamma refresh loop cancelled")
            raise
        except Exception:
            _logger.exception("gamma refresh loop error; backing off")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(_REFRESH_BACKOFF_S)
