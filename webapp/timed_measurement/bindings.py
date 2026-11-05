"""Bind future targets to exact original first publications, without new prices.

Only the worker calls discover. Invalid ownership remains unverified; it never
creates a requestable target. The bounded newest undiscovered publications take
priority, so accumulated history cannot hide today's calls after a restart.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp import signal_explanation
from webapp.alpha_lab.db import digest as opening_digest
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.opening import storage as opening_storage
from webapp.opening.model import Advice, clock, code_hash
from webapp.opening.model import settings as opening_settings

from . import storage
from .config import profile_hash, settings
from .model import Activation, Target, aware, digest, stored

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

STATE: dict[str, int | bool] = {}


def _object(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _json(raw: str) -> dict[str, object]:
    try:
        return _object(json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        return {}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _reference(raw: object, ticker: str, published: datetime) -> tuple[float, datetime] | None:
    point = _object(raw)
    price, at = _number(point.get("spot")), _timestamp(point.get("source_at"))
    cal = load_live_alpha_settings().calendar
    day = published.astimezone(ZoneInfo(cal.timezone)).date()
    if (price is None or at is None or point.get("session") != day.isoformat()
            or any(k in point and point[k] != ticker for k in ("ticker", "symbol"))
            or not 0 <= (published - at).total_seconds() <= opening_settings().source_max_age_seconds
            or at.astimezone(ZoneInfo(cal.timezone)).date() != day
            or classify(at, cal).mode != MarketMode.LIVE):
        return None
    return price, at


def window(published: datetime, minutes: int) -> tuple[datetime, datetime, bool, str]:
    """Original horizons/window, clipped at the real holiday/DST/half-day close."""
    published = aware(published)
    target = published + timedelta(minutes=minutes)
    deadline = target + timedelta(seconds=opening_settings().reference_window_seconds)
    original, future = clock(published), clock(target)
    if original.opening is None or original.closing is None or future.closing is None:
        return target, deadline, False, "unknown_calendar" if "takvim" in (original.reason + future.reason) else "outside_regular_session"
    deadline = min(deadline, original.closing)
    if (original.day != future.day or not original.opening <= published < original.closing
            or not original.opening < target < original.closing):
        return target, deadline, False, "outside_regular_session"
    return target, deadline, True, ""


def discover(engine: Engine, activation: Activation, now: datetime) -> tuple[Target, ...]:
    """Append bounded targets; no HTTP, old outcomes, or shared observations."""
    now = aware(now)
    cfg = opening_settings()
    STATE.clear()
    STATE.update(limited=False, candidates=0, verified=0, unverified=0)
    if (activation.profile_hash != profile_hash() or activation.opening_profile_hash != cfg.profile_hash
            or activation.activated_at > now):
        return ()
    required = (opening_storage.OpeningPrimary, opening_storage.OpeningDecision,
                opening_storage.OpeningCycle, LiveScan, storage.TimedTarget)
    names = set(inspect(engine).get_table_names())
    if any(table.__tablename__ not in names for table in required):
        return ()
    # Both horizons must exist before a publication is considered discovered.
    # A crash between the two inserts therefore leaves the missing one eligible
    # for discovery, while append-only conflict handling preserves the first.
    bound = (select(func.count()).select_from(storage.TimedTarget)
             .where(storage.TimedTarget.decision_key == opening_storage.OpeningPrimary.decision_key,
                    storage.TimedTarget.activation_key == activation.key)
             .correlate(opening_storage.OpeningPrimary).scalar_subquery())
    conditions = (opening_storage.OpeningPrimary.kind == "entry",
                  opening_storage.OpeningPrimary.published_at >= activation.activated_at,
                  opening_storage.OpeningPrimary.published_at <= now,
                  bound < len(cfg.intraday_minutes))
    with session_factory(engine)() as session:
        total = session.scalar(select(func.count()).select_from(opening_storage.OpeningPrimary).where(*conditions)) or 0
        primaries = session.scalars(select(opening_storage.OpeningPrimary).where(*conditions)
            .order_by(opening_storage.OpeningPrimary.published_at.desc(), opening_storage.OpeningPrimary.key)
            .limit(settings().read_limit)).all()
        keys = tuple(p.decision_key for p in primaries if p.decision_key)
        decisions = {r.key: r for r in session.scalars(select(opening_storage.OpeningDecision)
                     .where(opening_storage.OpeningDecision.key.in_(keys))).all()} if keys else {}
        cycles = {r.key: r for r in session.scalars(select(opening_storage.OpeningCycle)
                  .where(opening_storage.OpeningCycle.key.in_(tuple(p.cycle_key for p in primaries)))).all()} if primaries else {}
        scans = {r.scan_id: r for r in session.scalars(select(LiveScan)
                 .where(LiveScan.scan_id.in_(tuple(c.scan_id for c in cycles.values())))).all()} if cycles else {}
    STATE.update(limited=total > len(primaries), candidates=len(primaries))
    explanations = signal_explanation.build(engine, now, keys=keys)
    output: list[Target] = []
    verified = 0
    for primary in primaries:
        row, cycle = decisions.get(primary.decision_key or ""), cycles.get(primary.cycle_key)
        explanation = explanations.get(primary.decision_key or "")
        published = stored(primary.published_at)
        if row is None or cycle is None or explanation is None:
            continue
        metadata = _json(cycle.payload_json)
        if (primary.key != opening_digest((cfg.profile_hash, primary.day, "entry", primary.ticker))
                or row.key != primary.decision_key or row.cycle_key != primary.cycle_key
                or row.day != primary.day or cycle.day != primary.day or row.ticker != primary.ticker
                or row.action != "BUY" or stored(row.published_at) != published
                or stored(cycle.published_at) != published
                or metadata.get("profile_hash") != activation.opening_profile_hash
                or metadata.get("code_hash") != code_hash()
                or metadata.get("phase") != clock(published).phase
                or primary.day != clock(published).day
                or not explanation.source_valid or explanation.first_issued_at != published
                or explanation.statement_at != published):
            continue
        try:
            advice = Advice.model_validate_json(row.payload_json)
        except (ValueError, TypeError):
            continue
        scan = scans.get(cycle.scan_id)
        if (scan is None or scan.market_mode != "LIVE" or scan.status not in {"ok", "degraded"}
                or advice.direction not in {"up", "down"}):
            continue
        snapshot = _json(scan.snapshot_json)
        raw_cards = snapshot.get("cards")
        cards = [_object(c) for c in raw_cards] if isinstance(raw_cards, list) else []
        matching = [c for c in cards if c.get("ticker") == advice.ticker
                    and c.get("direction") == advice.direction and opening_digest(c) == advice.source_card_hash]
        if len(matching) != 1 or metadata.get("source_snapshot_hash") != opening_digest(snapshot):
            continue
        card = matching[0]
        prices = _object(snapshot.get("price_observations"))
        own, spy = _reference(prices.get(advice.ticker), advice.ticker, published), _reference(prices.get("SPY"), "SPY", published)
        if spy and spy[1] > stored(scan.finished_at):
            spy = None
        entry = advice.plan.entry if advice.plan else None
        entry_ok = (own is not None and advice.plan is not None and _number(entry) is not None
                    and advice.instrument == advice.plan.instrument and entry == round(own[0], 2)
                    and _timestamp(_object(card.get("stock_plan")).get("entry_ref_at")) == own[1]
                    and own[1] <= stored(scan.finished_at)
                    and advice.source_at is not None and advice.source_at.tzinfo is not None
                    and aware(advice.source_at) == own[1]
                    and _number(_object(card.get("stock_plan")).get("entry_ref")) == entry)
        verified += 1
        for minutes in cfg.intraday_minutes:
            target_at, deadline, applicable, reason = window(published, minutes)
            if not entry_ok:
                applicable, reason = False, "missing_entry_reference"
            elif applicable and now >= deadline:
                reason = "late_discovery"
            horizon = f"m{minutes}"
            target = Target(key=digest((activation.key, primary.key, horizon)), activation_key=activation.key,
                profile_hash=activation.profile_hash, opening_profile_hash=cfg.profile_hash,
                primary_key=primary.key, decision_key=row.key, ticker=advice.ticker, day=primary.day,
                direction="up" if advice.direction == "up" else "down", instrument=advice.instrument,
                published_at=published, entry_reference=entry if entry_ok else None,
                entry_source_at=own[1] if own and entry_ok else None,
                spy_entry=spy[0] if spy else None, spy_entry_source_at=spy[1] if spy else None,
                source_scan_id=cycle.scan_id, source_snapshot_hash=str(metadata["source_snapshot_hash"]),
                source_card_hash=advice.source_card_hash, horizon=horizon, horizon_minutes=minutes,
                target_at=target_at, deadline=deadline, eligible=applicable, reason=reason, discovered_at=now)
            if storage.save_target(engine, target):
                output.append(target)
    STATE.update(verified=verified, unverified=len(primaries) - verified)
    return tuple(output)
