"""Bounded SELECT-only export of machine-readable, causally known research inputs.

Any is restricted to immutable snapshot JSON. Legacy explanatory strings are
never parsed into features; an unavailable first observation stays unavailable.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from webapp.alpha_lab.flow import timestamp
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.news_research.config import profile_hash, settings
from webapp.news_research.experiment import Dataset, Opportunity, Price
from webapp.news_research.storage import evidence
from webapp.opening.model import settings as opening_settings
from webapp.opening.model import utc

from uoa_detector.live_alpha.calendar import (
    MarketMode,
    classify,
    close_time,
    is_session_day,
    previous_session_before,
)
from uoa_detector.live_alpha.settings import load_live_alpha_settings


@dataclass(frozen=True)
class NewsProvenance:
    opportunity_key: str
    packet_key: str | None
    payload_hash: str | None
    available_at: datetime | None
    flag: bool | None
    reason: str


@dataclass(frozen=True)
class ExportAudit:
    dataset: Dataset | None
    snapshots: int
    new_context_scans: int
    legacy_scans: int
    price_rows: int
    missing: tuple[tuple[str, int], ...]
    news_provenance: tuple[NewsProvenance, ...] = ()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _time(value: object) -> datetime | None:
    return timestamp(value) if isinstance(value, str) else None


def _and(left: bool | None, right: bool | None) -> bool | None:
    return False if left is False or right is False else True if left is True and right is True else None


def _flow(raw: object, at: datetime, opening: datetime) -> tuple[bool | None, str]:
    if not isinstance(raw, dict):
        return None, "flow_fields_unavailable"
    up, down, unknown = (_number(raw.get(k)) for k in ("premium_up", "premium_down", "premium_side_unknown"))
    contracts = _number(raw.get("distinct_contracts"))
    last = _time(raw.get("last_ts"))
    if any(v is None or v < 0 for v in (up, down, unknown, contracts)):
        return None, "flow_fields_invalid"
    assert up is not None and down is not None and unknown is not None and contracts is not None
    if contracts != int(contracts):
        return None, "flow_contract_count_invalid"
    cfg = load_live_alpha_settings().flow
    total, known = up + down + unknown, up + down
    passed = (up >= cfg.min_directional_premium_usd and known > 0 and total > 0
              and up / known >= cfg.dominance_min and known / total >= cfg.side_aware_share_min
              and contracts >= cfg.min_distinct_contracts)
    if not passed:
        return False, "flow_below_fixed_threshold"
    if last is None or last > at:
        return None, "flow_source_time_unavailable"
    if last < opening or (at - last).total_seconds() > settings().flow_max_age_seconds:
        return False, "flow_not_current"
    return True, ""


def _price(raw: object, day: date, at: datetime) -> tuple[tuple[float, float, float] | None, str]:
    if not isinstance(raw, dict) or raw.get("state") != "checked_found":
        return None, "price_context_unavailable"
    spot, prior, atr = (_number(raw.get(k)) for k in ("spot", "prev_close", "atr"))
    observed = _time(raw.get("spot_fetched_at"))
    cal = load_live_alpha_settings().calendar
    if any(v is None or v <= 0 for v in (spot, prior, atr)):
        return None, "price_context_invalid"
    if (observed is None or observed > at or (at - observed).total_seconds() > settings().source_max_age_seconds
            or _day(raw.get("source_session")) != day
            or _day(raw.get("prev_close_day")) != previous_session_before(day, cal)):
        return None, "price_source_session_or_freshness_unavailable"
    assert spot is not None and prior is not None and atr is not None
    return (spot / prior - 1, (spot - prior) / atr, spot), ""


def _opportunity(engine: Engine, row: LiveScan, snap: dict[str, Any], ticker: str, at: datetime, day: date,
                 opening: datetime, missing: Counter[str], provenance: list[NewsProvenance]) -> Opportunity:
    key = f"{row.scan_id}:{ticker}"
    cfg = load_live_alpha_settings()
    context = snap.get("news_research_context")
    if not isinstance(context, dict) or context.get("version") != "physical_news_v1":
        missing["legacy_unknown_roster"] += 1
        return Opportunity(key, ticker, day, at, None, None, None, ("legacy_unknown_roster",))
    if (context.get("profile_hash") != profile_hash() or row.policy_version != cfg.policy_version
            or snap.get("policy_version") != cfg.policy_version or snap.get("profile_sha256") != cfg.profile_sha256):
        missing["source_profile_unverified"] += 1
        return Opportunity(key, ticker, day, at, None, None, None, ("source_profile_unverified",))
    problems: list[str] = []
    flows = snap.get("flow_observations", {})
    passed, flow_reason = _flow(flows.get(ticker) if isinstance(flows, dict) else None, at, opening)
    if snap.get("flow_current") is not True:
        passed = False if snap.get("flow_current") is False else None
        flow_reason = "current_flow_identity_unavailable"
    if flow_reason:
        problems.append(flow_reason)
    prices = context.get("prices", {})
    raw = prices.get(ticker) if isinstance(prices, dict) else None
    price, price_reason = _price(raw, day, at)
    price_pass: bool | None = None if price is None else cfg.price.confirm_min_atr <= price[1] <= cfg.price.chase_max_atr
    if price_reason:
        problems.append(price_reason)
    elif not price_pass:
        problems.append("price_outside_fixed_confirmation_range")
    base = _and(passed, price_pass)
    relative: bool | None = None
    benchmark = prices.get(cfg.price.benchmark) if isinstance(prices, dict) else None
    reference, _ = _price(benchmark, day, at)
    provided = _number(raw.get("benchmark_move")) if isinstance(raw, dict) else None
    if price is not None and reference is not None and provided is not None and math.isclose(provided, reference[0], rel_tol=1e-9, abs_tol=1e-12):
        relative = price[0] - reference[0] >= cfg.price.relative_min
    elif base is not False:
        problems.append("relative_strength_unavailable")
    news_flag: bool | None = None
    if base is not False:
        checks = snap.get("news_observations", {})
        check = checks.get(ticker) if isinstance(checks, dict) else None
        checked_at = _time(check.get("checked_at")) if isinstance(check, dict) else None
        if (not isinstance(check, dict) or check.get("state") not in ("checked_found", "checked_none")
                or checked_at is None or not 0 <= (at - checked_at).total_seconds() <= settings().news_max_age_seconds):
            problems.append("news_check_unavailable")
        else:
            observed = evidence(engine=engine, ticker=ticker, asof=at,
                                window_hours=settings().news_window_hours,
                                max_age_seconds=settings().news_max_age_seconds, check_at=checked_at)
            provenance.append(NewsProvenance(key, observed.packet_key, observed.payload_hash,
                                             observed.available_at, observed.flag, observed.reason))
            news_flag = observed.flag
            if observed.flag is None:
                problems.append(observed.reason)
    missing.update(problems)
    return Opportunity(key, ticker, day, at, relative, news_flag, base, tuple(problems))


def _validate_since(since: datetime | None, asof: datetime) -> None:
    if since is None:
        return
    since = utc(since)
    if since > asof:
        raise ValueError("export since bound is after asof")
    cal = load_live_alpha_settings().calendar
    local = since.astimezone(ZoneInfo(cal.timezone))
    if is_session_day(local.date(), cal) is None:
        raise ValueError("first-session calendar unavailable")
    if is_session_day(local.date(), cal):
        assessment = datetime.combine(local.date(), cal.regular_open, local.tzinfo) + timedelta(minutes=opening_settings().entry_delay_minutes)
        closing = datetime.combine(local.date(), close_time(local.date(), cal), local.tzinfo)
        if assessment < local < closing:
            raise ValueError("partial first-session export refused; --since must be at/before assessment or at/after close")


def _marks(row: LiveScan, snap: dict[str, Any], at: datetime,
           points: dict[tuple[str, datetime], Price], missing: Counter[str],
           count: int, bound: int) -> int:
    """Marks are cached source clocks, known no earlier than completed scan.

    Old alpha-lab observation clocks are not physical receipts and never supply
    strict labels. Entry baseline completeness is independent of usable marks.
    """
    cfg = load_live_alpha_settings()
    context = snap.get("news_research_context")
    if (not isinstance(context, dict) or context.get("version") != "physical_news_v1"
            or context.get("profile_hash") != profile_hash()
            or row.policy_version != cfg.policy_version
            or snap.get("policy_version") != cfg.policy_version
            or snap.get("profile_sha256") != cfg.profile_sha256):
        missing["mark_profile_or_machine_context_unavailable"] += 1
        return count
    raw_prices = context.get("prices")
    if not isinstance(raw_prices, dict):
        missing["mark_machine_prices_unavailable"] += 1
        return count
    day = at.astimezone(ZoneInfo(cfg.calendar.timezone)).date()
    for ticker, raw in sorted(raw_prices.items()):
        count += 1
        if count > bound:
            raise ValueError("mark export truncated; request a bounded --since interval")
        if not isinstance(ticker, str) or not ticker.strip() or not isinstance(raw, dict):
            missing["mark_identity_or_fields_unavailable"] += 1
            continue
        value, source = _number(raw.get("spot")), _time(raw.get("spot_fetched_at"))
        if (value is None or value <= 0 or source is None
                or not 0 <= (at - source).total_seconds() <= settings().source_max_age_seconds
                or _day(raw.get("source_session")) != day
                or source.astimezone(ZoneInfo(cfg.calendar.timezone)).date() != day):
            missing["mark_source_session_clock_or_value_unavailable"] += 1
            continue
        key = ticker.strip().upper(), source
        old = points.get(key)
        if old is not None and old.value != value:
            raise ValueError("conflicting machine-context price versions; export refused")
        if old is None or at < old.known_at:
            points[key] = Price(key[0], source, at, value)
    return count


def audit(engine: Engine, asof: datetime, *, since: datetime | None = None,
          limit: int | None = None) -> ExportAudit:
    """Export without writes, HTTP, label construction or truncation substitution."""
    if asof.tzinfo is None or (since is not None and since.tzinfo is None):
        raise ValueError("aware export bounds required")
    asof = utc(asof)
    _validate_since(since, asof)
    bound = settings().row_limit if limit is None else limit
    if bound < 1 or bound > settings().row_limit:
        raise ValueError("export row bound exceeds frozen protocol")
    names = set(inspect(engine).get_table_names())
    if LiveScan.__tablename__ not in names:
        return ExportAudit(None, 0, 0, 0, 0, (("scan_storage_unavailable", 1),))
    missing: Counter[str] = Counter()
    opportunities: list[Opportunity] = []
    sessions: set[date] = set()
    source_ids: list[tuple[str, str]] = []
    news_provenance: list[NewsProvenance] = []
    price_rows = 0
    points: dict[tuple[str, datetime], Price] = {}
    fresh = legacy = 0
    cfg = load_live_alpha_settings()
    with session_factory(engine)() as connection:
        query = select(LiveScan).where(LiveScan.finished_at <= asof)
        if since is not None:
            query = query.where(LiveScan.finished_at >= utc(since))
        rows = list(connection.scalars(query.order_by(LiveScan.finished_at, LiveScan.scan_id).limit(bound + 1)))
        if len(rows) > bound:
            raise ValueError("scan export truncated; request a bounded --since interval")
        for row in rows:
            at = utc(row.finished_at)
            if row.status not in ("ok", "degraded") or utc(row.started_at) > at:
                missing["scan_not_usable"] += 1
                continue
            try:
                snap: dict[str, Any] = json.loads(row.snapshot_json)
            except (ValueError, TypeError):
                missing["malformed_snapshot"] += 1
                continue
            if not isinstance(snap, dict):
                missing["malformed_snapshot"] += 1
                continue
            context = snap.get("news_research_context")
            if isinstance(context, dict) and context.get("version") == "physical_news_v1":
                fresh += 1
            else:
                legacy += 1
            source_ids.append((row.scan_id, hashlib.sha256(row.snapshot_json.encode()).hexdigest()))
            # Even pre-assessment/no-opportunity snapshots can contain later marks.
            # Their evidence and hashes remain part of the exported source prefix.
            price_rows = _marks(row, snap, at, points, missing, price_rows, bound)
            market = classify(at, cfg.calendar)
            opening = datetime.combine(market.et_now.date(), cfg.calendar.regular_open, ZoneInfo(cfg.calendar.timezone))
            if (row.market_mode != "LIVE" or market.mode is not MarketMode.LIVE
                    or at < opening + timedelta(minutes=opening_settings().entry_delay_minutes)):
                missing["outside_assessment_session"] += 1
                continue
            if (at - utc(row.started_at)).total_seconds() > settings().source_max_age_seconds:
                missing["scan_clock_invalid_or_stale"] += 1
                continue
            flow = snap.get("flow_observations", {})
            if not isinstance(flow, dict):
                missing["flow_universe_unavailable"] += 1
                continue
            for ticker in sorted(flow):
                if not isinstance(ticker, str) or not ticker:
                    raise ValueError("invalid ticker identity in snapshot")
                opportunity = _opportunity(engine, row, snap, ticker, at, market.et_now.date(), opening, missing, news_provenance)
                opportunities.append(opportunity)
                # Without full-session poll coverage, an isolated low-flow scan
                # cannot certify a zero-exposure cash day. Count roster days only.
                if opportunity.base is not False:
                    sessions.add(market.et_now.date())
                if len(opportunities) > bound:
                    raise ValueError("opportunity export truncated; request a bounded --since interval")
    if not points:
        missing["strict_scan_marks_unavailable"] += 1
    identity = hashlib.sha256(json.dumps({"profile": profile_hash(), "live": cfg.profile_sha256,
        "sources": source_ids, "prices": [(p.ticker, p.at.isoformat(), p.known_at.isoformat(), p.value) for p in points.values()],
        "news": [asdict(item) for item in news_provenance],
        "opportunities": [asdict(item) for item in opportunities],
        "asof": asof.isoformat()}, sort_keys=True, default=str).encode()).hexdigest()
    dataset = Dataset(tuple(opportunities), tuple(points.values()), tuple(sorted(sessions)), identity, asof)
    return ExportAudit(dataset, len(rows), fresh, legacy, price_rows, tuple(sorted(missing.items())),
                       tuple(news_provenance))


def export(engine: Engine, asof: datetime, *, since: datetime | None = None,
           limit: int | None = None) -> Dataset:
    result = audit(engine, asof, since=since, limit=limit)
    if result.dataset is None:
        raise ValueError("research source storage unavailable; no zero-opportunity inference")
    return result.dataset
