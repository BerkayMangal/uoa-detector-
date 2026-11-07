"""Capture the actual stored source before outcomes; no retrospective feature fill."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from webapp.board.db import session_factory
from webapp.fusion_live.model import FundamentalEvidence, NewsEvidence
from webapp.fusion_research.model import FeatureRow, Observation
from webapp.gamma import GammaRow
from webapp.live_alpha.store import LiveScan
from webapp.shadow_capture.model import Record, aware, digest, stored


def source_started(engine: Engine, candidate: Record) -> datetime | None:
    scan_id = candidate.payload.get("scan_id")
    if not isinstance(scan_id, str):
        return None
    with session_factory(engine)() as s:
        at = s.scalar(select(LiveScan.started_at).where(LiveScan.scan_id == scan_id))
    return stored(at) if at is not None else None


def snapshot(engine: Engine, candidate: Record, now: datetime) -> dict[str, Any]:
    """Untyped immutable provider/source boundary, retained privately inside claim."""
    with session_factory(engine)() as s:
        row = s.get(LiveScan, candidate.payload.get("scan_id"))
    if row is None:
        raise ValueError("fusion_source_missing")
    import hashlib

    if hashlib.sha256(row.snapshot_json.encode()).hexdigest() != candidate.payload.get(
        "snapshot_hash"
    ):
        raise ValueError("fusion_source_changed")
    data = json.loads(row.snapshot_json)
    ticker = str(candidate.payload["ticker"])
    context = data.get("news_research_context") or {}
    prices = context.get("prices") or {}
    out = {
        "flow": (data.get("flow_observations") or {}).get(ticker, {}),
        "price": prices.get(ticker, {}),
        "benchmark": prices.get("SPY", {}),
        "source_hash": candidate.payload["snapshot_hash"],
        "gamma": None,
        "observed_at": aware(now).isoformat(),
    }
    # A date-only cache can provide newly recorded daily context, never a precise
    # intraday quote. Its first knowability for this engine is the claim receipt.
    if inspect(engine).has_table(GammaRow.__tablename__):
        with session_factory(engine)() as s:
            gamma = s.get(GammaRow, ticker)
        if gamma is not None:
            try:
                day = date.fromisoformat(gamma.as_of[:10])
            except (ValueError, TypeError):
                day = None
            if day == candidate.day:
                out["gamma"] = {
                    "as_of": gamma.as_of,
                    "net_gex": _number(gamma.net_gex),
                    "flip": _number(gamma.flip),
                    "call_wall": _number(gamma.call_wall),
                    "put_wall": _number(gamma.put_wall),
                    "atm_iv": _number(gamma.atm_iv),
                    "iv_percentile": _number(gamma.iv_pct),
                    "quality": "date_only_recorded_context",
                }
    return out


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        n = float(value)
    except (ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def features(
    candidate: Record,
    raw: Mapping[str, Any],
    source_available: datetime,
    now: datetime,
    news: NewsEvidence,
    fundamental: FundamentalEvidence,
) -> FeatureRow:
    at, source_available = aware(now), aware(source_available)
    source_hash = str(raw.get("source_hash") or candidate.checksum)

    def observed(
        name: str,
        value: object,
        *,
        source: str = "completed_live_scan",
        known: datetime | None = source_available,
        sha: str = source_hash,
    ) -> Observation:
        if (
            value is None
            or known is None
            or known > at
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            return Observation(name=name, missing=("not_physically_available",))
        if not isinstance(value, str | int | float | bool):
            return Observation(name=name, missing=("unsupported_source_type",))
        return Observation(name=name, value=value, known_at=known, source=source, source_hash=sha)

    price_value, flow_value = raw.get("price"), raw.get("flow")
    price = price_value if isinstance(price_value, dict) else {}
    flow = flow_value if isinstance(flow_value, dict) else {}
    spot, previous, atr = (_number(price.get(n)) for n in ("spot", "prev_close", "atr"))
    move = (
        spot / previous - 1 if spot is not None and previous is not None and previous > 0 else None
    )
    move_atr = (
        (spot - previous) / atr
        if spot is not None and previous is not None and atr is not None and atr > 0
        else None
    )
    up, down = _number(flow.get("premium_up")), _number(flow.get("premium_down"))
    dominance = up / (up + down) if up is not None and down is not None and up + down > 0 else None
    benchmark = _number(price.get("benchmark_move"))
    gamma_value = raw.get("gamma")
    gamma = gamma_value if isinstance(gamma_value, dict) else {}
    event_at = aware(datetime.fromisoformat(str(candidate.payload["scan_finished_at"])))
    news_hash = news.source_hash or digest((news.model, news.prompt_hash, news.schema_hash))
    fundamental_hash = digest(fundamental.source_hashes)
    if candidate.day is None:
        raise ValueError("fusion_source_day_missing")
    return FeatureRow(
        event_id=candidate.key,
        ticker=str(candidate.payload["ticker"]),
        session=candidate.day,
        event_at=event_at,
        known_at=at,
        technical=(
            observed("move_pct", move * 100 if move is not None else None),
            observed("move_atr", move_atr),
            observed("atr", atr),
            observed("intraday_volume", _number(price.get("volume"))),
        ),
        flow=(
            observed("premium_up_usd", up),
            observed("premium_down_usd", down),
            observed("up_share", dominance),
            observed("distinct_up_contracts", _number(flow.get("distinct_up"))),
        ),
        market=(
            observed(
                "relative_pp",
                (move - benchmark) * 100 if move is not None and benchmark is not None else None,
            ),
            observed(
                "daily_net_gex",
                _number(gamma.get("net_gex")),
                source="date_only_gamma_context",
                sha=digest(gamma),
            ),
            observed(
                "daily_gamma_flip",
                _number(gamma.get("flip")),
                source="date_only_gamma_context",
                sha=digest(gamma),
            ),
            observed(
                "daily_iv_percentile",
                _number(gamma.get("iv_percentile")),
                source="date_only_gamma_context",
                sha=digest(gamma),
            ),
            observed("dark_pool_confirmation", None),
            observed("level2_orderbook", None),
        ),
        news=(
            observed(
                "positive",
                news.positive,
                source="semantic_news_v1",
                known=news.available_at,
                sha=news_hash,
            ),
            observed(
                "veto_accept",
                news.veto_accept,
                source="semantic_news_v1",
                known=news.available_at,
                sha=news_hash,
            ),
        ),
        fundamental=(
            observed(
                "revenue_yoy_pct",
                fundamental.revenue_yoy_pct,
                source="fundamental_context_v1",
                known=fundamental.available_at,
                sha=fundamental_hash,
            ),
            observed(
                "operating_margin_pct",
                fundamental.operating_margin_pct,
                source="fundamental_context_v1",
                known=fundamental.available_at,
                sha=fundamental_hash,
            ),
        ),
    )
