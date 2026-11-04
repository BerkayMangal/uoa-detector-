"""Read-only private provenance view. Never fetches, creates tables or evaluates."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, defer

from webapp.board.db import session_factory

from . import storage
from .config import Endpoint, enabled, profile_hash, settings
from .model import (
    FeedState,
    Item,
    SharedNewsView,
    Story,
    aware,
    parse,
    same_etag,
    stored,
    story_key,
)


def _first_known(session: Session, item: Item, now: datetime) -> datetime | None:
    pairs = session.execute(select(storage.Packet, storage.Receipt).options(defer(storage.Packet.compressed_body))
                            .join(storage.Observation, storage.Observation.packet_key == storage.Packet.key)
                            .join(storage.Receipt, storage.Receipt.key == storage.Packet.key)
                            .where(storage.Observation.provider == item.provider,
                                   storage.Observation.item_id == item.id,
                                   storage.Observation.version_hash == item.content_version_hash,
                                   storage.Receipt.available_at <= now)
                            .order_by(storage.Receipt.available_at, storage.Packet.key)
                            .limit(settings().read_packet_limit)).all()
    for packet, receipt in pairs:
        if packet.category == "ok" and storage.valid_receipt(packet, receipt, now):
            return stored(receipt.available_at)
    return None


def _feed(engine: Engine, endpoint: Endpoint, now: datetime) -> FeedState:
    base = dict(provider=endpoint.provider, coverage=endpoint.coverage)
    with session_factory(engine)() as session:
        rows = session.execute(select(storage.Packet, storage.Receipt).options(defer(storage.Packet.compressed_body))
                               .outerjoin(storage.Receipt, storage.Receipt.key == storage.Packet.key)
                               .where(storage.Packet.provider == endpoint.provider,
                                      storage.Packet.requested_at <= now, storage.Packet.received_at <= now)
                               .order_by(storage.Packet.requested_at.desc(), storage.Packet.key.desc())
                               .limit(settings().read_packet_limit)).all()
        if not rows:
            return FeedState(**base, status="unknown", reason="not_observed")
        latest, receipt = rows[0]
        attempt = stored(latest.requested_at)
        successes = session.execute(select(storage.Packet, storage.Receipt).options(defer(storage.Packet.compressed_body))
                                    .join(storage.Receipt, storage.Receipt.key == storage.Packet.key)
                                    .where(storage.Packet.provider == endpoint.provider,
                                           storage.Packet.category.in_(("ok", "not_modified")),
                                           storage.Receipt.available_at <= now)
                                    .order_by(storage.Receipt.available_at.desc(), storage.Packet.key.desc())
                                    .limit(settings().read_packet_limit)).all()
        success_at = next((stored(r.available_at) for p, r in successes
                           if p.category in {"ok", "not_modified"} and storage.valid_receipt(p, r, now)), None)
        fields = dict(last_attempt_at=attempt, last_successful_check_at=success_at, packet_key=latest.key)
        failed = storage.failure_at(engine, endpoint.provider)
        if failed is not None and attempt <= failed <= now:
            return FeedState(**base, **fields, status="unknown", reason="recording_unavailable")
        if not storage.valid_receipt(latest, receipt, now):
            return FeedState(**base, **fields, status="unknown", reason="receipt_pending_or_invalid")
        if latest.category not in {"ok", "not_modified"}:
            return FeedState(**base, **fields, status="unknown", reason=latest.category)
        original = latest
        original_receipt = receipt
        if latest.category == "not_modified":
            original = session.get(storage.Packet, latest.body_packet_key)
            original_receipt = session.get(storage.Receipt, latest.body_packet_key)
            if (original is None or original.provider != endpoint.provider or original.category != "ok"
                    or not original.etag or (latest.etag is not None and not same_etag(latest.etag, original.etag))
                    or not storage.valid_receipt(original, original_receipt, now)
                    or original_receipt is None or stored(original_receipt.available_at) > attempt):
                return FeedState(**base, **fields, status="unknown", reason="orphan_not_modified")
        if original_receipt is None:
            return FeedState(**base, **fields, status="unknown", reason="receipt_pending_or_invalid")
        parsed = parse(storage.body(original), endpoint, stored(original.received_at))
        observed = {o.item_id + ":" + o.version_hash: o for o in session.scalars(
            select(storage.Observation).where(storage.Observation.packet_key == original.key)).all()}
        items: list[Item] = []
        for item in parsed.items:
            observation = observed.get(item.id + ":" + item.content_version_hash)
            if observation is None or observation.item_json != item.model_dump_json():
                return FeedState(**base, **fields, status="unknown", reason="item_integrity_failure")
            first = _first_known(session, item, now)
            if first is None:
                return FeedState(**base, **fields, status="unknown", reason="item_receipt_missing")
            items.append(item.model_copy(update={"first_known_at": first, "packet_key": original.key}))
        stale = (now - stored(receipt.available_at)).total_seconds() > settings().max_age_seconds
        state = "stale" if stale else "context" if items else "unknown"
        reason = "physical_collection_stale" if stale else "curated_context_only" if items else "empty_curated_unknown"
        return FeedState(**base, **fields, status=state, reason=reason,
                         upstream_status=parsed.upstream_status,
                         last_new_content_at=max((i.first_known_at for i in items if i.first_known_at is not None), default=None), body_hash=original.body_hash,
                         returned_count=parsed.returned_count, limited=parsed.limited, truncated=parsed.truncated,
                         items=tuple(items))


def build(engine: Engine, now: datetime) -> SharedNewsView:
    now = aware(now)
    active = enabled()
    feeds: list[FeedState] = []
    try:
        names = set(inspect(engine).get_table_names()) if active else set()
        ready = all(t.__tablename__ in names for t in (storage.Packet, storage.Receipt, storage.Observation))
    except Exception:
        ready = False
    for endpoint in settings().providers:
        if not active or not ready:
            feeds.append(FeedState(provider=endpoint.provider, coverage=endpoint.coverage, status="disabled" if not active else "unknown",
                                   reason="collector_disabled" if not active else "storage_unavailable"))
            continue
        try:
            feeds.append(_feed(engine, endpoint, now))
        except Exception:
            # No SQL/network exception detail may contain connection credentials.
            feeds.append(FeedState(provider=endpoint.provider, coverage=endpoint.coverage,
                                   status="unknown", reason="stored_evidence_unavailable"))
    groups: dict[str, list[Item]] = {}
    for feed in feeds:
        for item in feed.items:
            groups.setdefault(story_key(item.url), []).append(item)
    stories = tuple(Story(canonical_url=url, versions=tuple(sorted(versions, key=lambda i: (i.provider, i.id, i.content_version_hash))))
                    for url, versions in sorted(groups.items()))
    return SharedNewsView(as_of=now, enabled=active, profile_hash=profile_hash(), feeds=tuple(feeds), stories=stories)
