"""Read existing physical source evidence; no vendor requests or old labels."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from webapp.board.db import session_factory
from webapp.news_research import storage as physical
from webapp.semantic_news import config
from webapp.semantic_news.model import EmptySource, Headline, Request, Source, aware, digest, stored
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow
from webapp.shadow_capture.model import Record


def candidates(engine: Engine, now: datetime) -> tuple[Record, ...]:
    if not shadow.available(engine):
        return ()
    from zoneinfo import ZoneInfo

    day = aware(now).astimezone(ZoneInfo("America/New_York")).date()
    return shadow.list_events(engine, "candidate", now, day=day, limit=5)


def source(engine: Engine, candidate: Record, now: datetime) -> Source:
    """One original source-bound packet, interpreted only at the current clock."""
    at = aware(now)
    active = shadow.active(engine, at)
    if (
        active is None
        or candidate.confirmed_at is None
        or candidate.day is None
        or not bindings.valid_candidate(engine, candidate, active, at)
    ):
        raise ValueError("candidate_unverified")
    from webapp.shadow_capture.config import settings as shadow_settings

    target = candidate.confirmed_at + timedelta(seconds=shadow_settings().entry_delay_seconds)
    if at > target - timedelta(seconds=config.settings().min_completion_lead_seconds):
        raise ValueError("entry_cutoff_passed")
    key, ticker = candidate.payload.get("news_packet_key"), candidate.payload.get("ticker")
    if (
        not isinstance(key, str)
        or not isinstance(ticker, str)
        or candidate.payload.get("news") is not True
    ):
        raise ValueError("source_headlines_unknown_or_empty")
    names = set(inspect(engine).get_table_names())
    if not all(t.__tablename__ in names for t in (physical.Packet, physical.Receipt)):
        raise ValueError("physical_news_storage_unavailable")
    with session_factory(engine)() as session:
        packet = session.get(physical.Packet, key)
    if (
        packet is None
        or packet.ticker != ticker
        or packet.payload_hash != candidate.payload.get("news_payload_hash")
    ):
        raise ValueError("source_packet_unverified")
    state = physical.evidence(
        engine,
        ticker,
        candidate.at,
        window_hours=config.settings().news_window_hours,
        check_at=stored(packet.application_checked_at) if packet.application_checked_at else None,
    )
    if (
        state.flag is not True
        or state.packet_key != key
        or state.available_at is None
        or state.payload_hash != packet.payload_hash
        or state.available_at > candidate.at
    ):
        raise ValueError("physical_news_unverified")
    # Eight MiB is the unchanged physical-source storage safety limit, not the
    # outgoing16KiB interpretation packet. Validate bytes before JSON adaptation.
    with gzip.GzipFile(fileobj=io.BytesIO(packet.compressed_payload)) as f:
        raw = f.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != packet.payload_hash:
        raise ValueError("source_hash_invalid")
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("source_shape_invalid")
    selected: dict[str, tuple[datetime, Headline]] = {}
    lower = candidate.at - timedelta(hours=config.settings().news_window_hours)
    for item in body["data"]:
        if not isinstance(item, dict):
            raise ValueError("source_item_invalid")
        title, publisher, tickers = item.get("headline"), item.get("source"), item.get("tickers")
        publication = physical.publication(item.get("created_at"))
        if (
            not isinstance(title, str)
            or not isinstance(publisher, str)
            or publication is None
            or not isinstance(tickers, list)
            or ticker not in {str(t).strip().upper() for t in tickers}
        ):
            continue
        title = title.strip()
        if title not in state.headlines or not lower <= publication <= candidate.at:
            continue
        if (
            not title
            or len(title) > config.settings().max_headline_characters
            or len(publisher) > 256
        ):
            continue  # Coverage is explicitly limited; never truncate a headline's meaning.
        normalized = re.sub(r"[^\w]+", "", title.casefold())
        item_id = digest((key, packet.payload_hash, title, publisher, publication.isoformat()))
        value = Headline(
            id=item_id, headline=title, source=publisher, published_at=publication.isoformat()
        )
        if normalized not in selected or publication < selected[normalized][0]:
            selected[normalized] = (publication, value)
    items = tuple(
        v
        for _, v in sorted(selected.values(), key=lambda x: (x[0], x[1].id), reverse=True)[
            : config.settings().max_items
        ]
    )
    if not items:
        raise ValueError("no_bounded_source_headlines")
    source_hash = digest(
        {
            "candidate_key": candidate.key,
            "candidate_checksum": candidate.checksum,
            "packet_key": key,
            "payload_hash": packet.payload_hash,
        }
    )
    core = {
        "version": config.settings().version,
        "ticker": ticker,
        "source_hash": source_hash,
        "items": [i.model_dump() for i in items],
    }
    request = Request(
        version="semantic_news_v1",
        request_id=digest(core),
        ticker=ticker,
        source_hash=source_hash,
        items=items,
    )
    from webapp.semantic_news.model import canonical

    if (
        not request.valid(at)
        or len(canonical(request.model_dump(mode="json"))) > config.settings().max_input_bytes
    ):
        raise ValueError("request_unverified_or_oversize")
    return Source(
        candidate.key,
        candidate.checksum,
        ticker,
        candidate.day,
        candidate.confirmed_at,
        target,
        key,
        packet.payload_hash,
        state.available_at,
        request,
        len(state.headlines),
    )


def empty_source(engine: Engine, candidate: Record, now: datetime) -> EmptySource | None:
    """Known checked-empty is evidence; missing or truncated data is not empty."""
    if candidate.payload.get("news") is not False:
        return None
    at = aware(now)
    active = shadow.active(engine, at)
    if (
        active is None
        or candidate.confirmed_at is None
        or candidate.day is None
        or not bindings.valid_candidate(engine, candidate, active, at)
    ):
        return None
    from webapp.shadow_capture.config import settings as shadow_settings

    target = candidate.confirmed_at + timedelta(seconds=shadow_settings().entry_delay_seconds)
    if at > target - timedelta(seconds=config.settings().min_completion_lead_seconds):
        return None
    key, ticker = candidate.payload.get("news_packet_key"), candidate.payload.get("ticker")
    if not isinstance(key, str) or not isinstance(ticker, str):
        return None
    with session_factory(engine)() as session:
        packet = session.get(physical.Packet, key)
    if (
        packet is None
        or packet.ticker != ticker
        or packet.payload_hash != candidate.payload.get("news_payload_hash")
    ):
        return None
    state = physical.evidence(
        engine,
        ticker,
        candidate.at,
        window_hours=config.settings().news_window_hours,
        check_at=stored(packet.application_checked_at) if packet.application_checked_at else None,
    )
    if (
        state.flag is not False
        or state.packet_key != key
        or state.available_at is None
        or state.available_at > candidate.at
        or state.payload_hash != packet.payload_hash
    ):
        return None
    with gzip.GzipFile(fileobj=io.BytesIO(packet.compressed_payload)) as f:
        raw = f.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != packet.payload_hash:
        return None
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    from uoa_detector.live_alpha.settings import load_live_alpha_settings

    if len(body["data"]) >= load_live_alpha_settings().news.limit or any(
        body.get(k) not in (None, False, "", 0)
        for k in ("limited", "truncated", "has_more", "next", "next_cursor", "next_page")
    ):
        return None
    return EmptySource(
        candidate.key,
        candidate.checksum,
        ticker,
        candidate.day,
        target,
        key,
        packet.payload_hash,
        state.available_at,
        "known_empty" if not body["data"] else "no_fresh_news",
    )
