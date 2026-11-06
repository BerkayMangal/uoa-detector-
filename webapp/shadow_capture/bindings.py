"""Observe newly completed, known-base sources without changing live advice.

Snapshot JSON is an untrusted adapter boundary. Source clocks, gates and news
provenance retain the original frozen adapter's meaning; admission is a new
prospective observation, never a repaired historical first opportunity.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.news_research import adapter
from webapp.news_research.config import profile_hash as news_profile_hash
from webapp.news_research.config import settings as news_settings
from webapp.opening.model import settings as opening_settings
from webapp.shadow_capture import config, storage
from webapp.shadow_capture.model import Record, aware, digest, stored

_TICKER = re.compile(r"[A-Z][A-Z0-9.-]*", re.ASCII)


def candidate_key(day: date, slot: int) -> str:
    return digest(('candidate_slot', config.settings().version, day.isoformat(), slot))


def candidate_owner(day: date, ticker: str) -> str:
    return digest(('candidate_ticker', config.settings().version, day.isoformat(), ticker))


def _current(at: datetime, previous: datetime, finished: datetime, day: date) -> datetime:
    at = aware(at)
    market = classify(at, load_live_alpha_settings().calendar)
    if (at < previous or not 0 <= (at - finished).total_seconds() <= config.settings().source_max_age_seconds
            or market.mode is not MarketMode.LIVE or market.et_now.date() != day):
        raise ValueError('source_publication_clock_invalid')
    return at


def _snapshot(row: LiveScan) -> dict[str, Any] | None:
    # Any is confined to immutable source snapshot JSON at this adapter boundary.
    try:
        value = json.loads(row.snapshot_json)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    live = load_live_alpha_settings()
    context = value.get('news_research_context')
    if (not isinstance(context, dict) or context.get('version') != 'physical_news_v1'
            or context.get('profile_hash') != news_profile_hash()
            or row.policy_version != live.policy_version
            or value.get('policy_version') != live.policy_version
            or value.get('profile_sha256') != live.profile_sha256):
        return None
    return value


def _existing(engine: Engine, at: datetime, day: date) -> tuple[Record, ...]:
    # Unconfirmed parents also occupy their immutable day/slot/ticker. A crash
    # must not free a slot or allow a later opportunity to replace that record.
    return storage.list_events(engine, 'candidate', at, day=day,
                               limit=config.settings().candidate_slots + 1,
                               require_receipt=False)


def valid_candidate(engine: Engine, candidate: Record, activation: Record, now: datetime) -> bool:
    """Revalidate original admission, not the source's age at a later outcome.

    Every source feature/news flag is recomputed at the immutable scan finish.
    The candidate's clocks certify its original prospective publication; a valid
    reference sixty minutes later does not make that original source stale.
    """
    at = aware(now)
    try:
        aware(candidate.at)
        if candidate.confirmed_at is not None:
            aware(candidate.confirmed_at)
        if activation.confirmed_at is not None:
            aware(activation.confirmed_at)
    except ValueError:
        return False
    if (candidate.kind != 'candidate' or activation.kind != 'activation'
            or candidate.day is None or candidate.confirmed_at is None
            or activation.confirmed_at is None or candidate.confirmed_at > at
            or candidate.parent_key != activation.key
            or candidate.payload.get('activation_key') != activation.key
            or candidate.payload.get('identity') != config.identity()
            or activation.payload != config.identity()
            or storage.active(engine, at) != activation
            or storage.get(engine, candidate.key, at) != candidate):
        return False
    payload = candidate.payload
    ticker, slot, scan_id = payload.get('ticker'), payload.get('slot'), payload.get('scan_id')
    if (not isinstance(ticker, str) or not _TICKER.fullmatch(ticker)
            or ticker == load_live_alpha_settings().price.benchmark
            or type(slot) is not int or not 1 <= slot <= config.settings().candidate_slots
            or candidate.key != candidate_key(candidate.day, slot)
            or candidate.unique_owner != candidate_owner(candidate.day, ticker)
            or not isinstance(scan_id, str) or not scan_id
            or payload.get('base') is not True or 'rs' not in payload or 'news' not in payload):
        return False
    if LiveScan.__tablename__ not in inspect(engine).get_table_names():
        return False
    with session_factory(engine)() as session:
        row = session.get(LiveScan, scan_id)
    if row is None:
        return False
    started, finished = stored(row.started_at), stored(row.finished_at)
    if (row.market_mode != 'LIVE' or row.status not in {'ok', 'degraded'}
            or started < activation.confirmed_at or not started <= finished <= candidate.at
            or (finished-started).total_seconds() > news_settings().source_max_age_seconds
            or payload.get('snapshot_hash') != hashlib.sha256(row.snapshot_json.encode()).hexdigest()
            or payload.get('scan_finished_at') != finished.isoformat()):
        return False
    market = classify(finished, load_live_alpha_settings().calendar)
    zone = ZoneInfo(load_live_alpha_settings().calendar.timezone)
    opening = datetime.combine(candidate.day, load_live_alpha_settings().calendar.regular_open, zone)
    if (market.mode is not MarketMode.LIVE or market.et_now.date() != candidate.day
            or started < opening + timedelta(minutes=opening_settings().entry_delay_minutes)):
        return False
    try:
        _current(candidate.at, finished, finished, candidate.day)
        _current(candidate.confirmed_at, candidate.at, finished, candidate.day)
    except ValueError:
        return False
    snap = _snapshot(row)
    if snap is None:
        return False
    flow = snap.get('flow_observations')
    if not isinstance(flow, dict) or ticker not in flow:
        return False
    provenance: list[adapter.NewsProvenance] = []
    item = adapter._opportunity(engine, row, snap, ticker, finished, candidate.day,
                                opening, Counter(), provenance)
    evidence = next((p for p in reversed(provenance) if p.opportunity_key == item.key), None)
    return (item.base is True and item.rs is payload.get('rs') and item.news is payload.get('news')
            and payload.get('source_missing') == list(item.missing)
            and payload.get('news_packet_key') == (evidence.packet_key if evidence else None)
            and payload.get('news_payload_hash') == (evidence.payload_hash if evidence else None)
            and payload.get('news_available_at') == (
                evidence.available_at.isoformat() if evidence and evidence.available_at else None))


def discover(engine: Engine, now: datetime, *, clock: Callable[[], datetime]) -> dict[str, str | int | bool]:
    """Worker-only bounded observation. No HTTP, schema changes or old mark reads."""
    at = aware(now)
    result: dict[str, str | int | bool] = {
        'status': 'no_source', 'source_scans': 0, 'candidates': 0,
        'admitted': 0, 'unknown': 0, 'limited': False,
    }
    moment = aware(clock())
    if moment < at:
        return result | {'status': 'clock_error'}
    active = storage.active(engine, moment)
    if active is None or active.confirmed_at is None:
        return result | {'status': 'activation_unavailable'}
    if LiveScan.__tablename__ not in inspect(engine).get_table_names():
        return result | {'status': 'source_storage_unavailable'}
    # Most polls see the same source. Read only its small timing/status fields
    # until freshness, the durable processed marker and day capacity pass.
    with session_factory(engine)() as session:
        latest = session.execute(select(LiveScan.scan_id, LiveScan.started_at,
            LiveScan.finished_at, LiveScan.market_mode, LiveScan.status)
            .where(LiveScan.finished_at <= moment)
            .order_by(LiveScan.finished_at.desc(), LiveScan.scan_id.desc()).limit(1)).first()
    if latest is None:
        return result
    scan_id, source_started, source_finished, source_mode, source_status = latest
    result['source_scans'] = 1
    started, finished = stored(source_started), stored(source_finished)
    market = classify(finished, load_live_alpha_settings().calendar)
    day = market.et_now.date()
    zone = ZoneInfo(load_live_alpha_settings().calendar.timezone)
    opening = datetime.combine(day, load_live_alpha_settings().calendar.regular_open, zone)
    if (source_mode != 'LIVE' or source_status not in {'ok', 'degraded'}
            or market.mode is not MarketMode.LIVE
            or started < active.confirmed_at or started > finished
            or (finished - started).total_seconds() > news_settings().source_max_age_seconds
            or started < opening + timedelta(minutes=opening_settings().entry_delay_minutes)):
        return result | {'status': 'source_ineligible'}
    try:
        moment = _current(moment, moment, finished, day)
    except ValueError:
        return result | {'status': 'source_expired'}
    marker = digest((config.settings().version, active.key, scan_id, 'discovery'))
    if storage.get(engine, marker, moment, require_receipt=False) is not None:
        return result | {'status': 'already_observed'}
    prior = _existing(engine, moment, day)
    if len(prior) >= config.settings().candidate_slots:
        return result | {'status': 'candidate_limit', 'limited': True}
    with session_factory(engine)() as session:
        row = session.get(LiveScan, scan_id)
    if (row is None or row.started_at != source_started or row.finished_at != source_finished
            or row.market_mode != source_mode or row.status != source_status):
        return result | {'status': 'source_changed'}
    snap = _snapshot(row)
    if snap is None:
        return result | {'status': 'source_unverified'}
    flows = snap.get('flow_observations')
    if not isinstance(flows, dict) or any(not isinstance(t, str) or not _TICKER.fullmatch(t) for t in flows):
        return result | {'status': 'source_universe_invalid'}
    if len(flows) > config.settings().source_row_limit:
        return result | {'status': 'source_limit', 'limited': True}
    seen = {record.unique_owner for record in prior}
    checksum = hashlib.sha256(row.snapshot_json.encode()).hexdigest()
    missing: Counter[str] = Counter()
    provenance: list[adapter.NewsProvenance] = []
    for ticker in sorted(flows):
        if ticker == load_live_alpha_settings().price.benchmark:
            continue
        item = adapter._opportunity(engine, row, snap, ticker, finished, day, opening, missing, provenance)
        if item.base is None:
            result['unknown'] = int(result['unknown']) + 1
        if item.base is not True:
            continue
        result['candidates'] = int(result['candidates']) + 1
        owner = candidate_owner(day, ticker)
        if owner in seen:
            continue
        observed = next((p for p in reversed(provenance) if p.opportunity_key == item.key), None)
        for slot in range(1, config.settings().candidate_slots + 1):
            try:
                moment = _current(clock(), moment, finished, day)

                def receipt_clock() -> datetime:
                    nonlocal moment
                    moment = _current(clock(), moment, finished, day)
                    return moment

                saved = storage.put(engine, key=candidate_key(day, slot), kind='candidate', at=moment,
                    day=day, parent_key=active.key, unique_owner=owner,
                    payload={'identity': config.identity(), 'activation_key': active.key, 'slot': slot,
                        'ticker': ticker, 'scan_id': row.scan_id, 'snapshot_hash': checksum,
                        'scan_finished_at': finished.isoformat(), 'rs': item.rs,
                        'news': item.news, 'base': True, 'source_missing': list(item.missing),
                        'news_packet_key': observed.packet_key if observed else None,
                        'news_payload_hash': observed.payload_hash if observed else None,
                        'news_available_at': observed.available_at.isoformat() if observed and observed.available_at else None},
                    clock=receipt_clock)
            except ValueError:
                return result | {'status': 'publication_refused'}
            if saved is not None:
                result['admitted'] = int(result['admitted']) + 1
                seen.add(owner)
                break
            # A peer may have reserved this ticker under another slot. Refresh
            # only bounded candidate metadata before trying the next slot.
            current = _existing(engine, moment, day)
            seen.update(record.unique_owner for record in current)
            if owner in seen:
                break
    try:
        moment = _current(clock(), moment, finished, day)
        storage.put(engine, key=marker, kind='discovery', at=moment, day=day,
                    parent_key=active.key,
                    payload={'identity': config.identity(), 'activation_key': active.key,
                             'scan_id': row.scan_id, 'snapshot_hash': checksum,
                             'source_profile': 'physical_news_v1',
                             'source_finished_at': finished.isoformat(),
                             'source_scans': 1, 'candidates': int(result['candidates']),
                             'admitted': int(result['admitted']), 'unknown': int(result['unknown']),
                             'limited': False, 'missing': dict(sorted(missing.items()))},
                    clock=lambda: _current(clock(), moment, finished, day))
    except ValueError:
        return result | {'status': 'publication_refused'}
    return result | {'status': 'observed'}
