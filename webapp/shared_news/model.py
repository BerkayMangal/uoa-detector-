"""Typed private context and provenance. Absence is never negative evidence."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, JsonValue

from .config import Endpoint, Provider, settings


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware physical clock required")
    return value.astimezone(UTC)


def stored(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else aware(value)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def etag_value(value: str) -> str:
    match = re.fullmatch(r'(?:W/)?"([\x21\x23-\x7e]{1,240})"', value)
    if not match:
        raise ValueError("invalid ETag")
    return match.group(1)


def same_etag(first: str, second: str) -> bool:
    # If-None-Match uses weak comparison. Railway can remove/add W/ while
    # retaining the quoted opaque tag; all other bytes must still match.
    return etag_value(first) == etag_value(second)


def article_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        raise ValueError("invalid publisher URL")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid publisher URL")
    # Links are displayed, never requested by this adapter.
    return value


def story_key(value: str) -> str:
    parts = urlsplit(value)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "cmpid", "shareid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Item(Frozen):
    provider: Provider
    id: str
    content_version_hash: str
    source_id: str | None
    source: str | None
    source_title: str | None
    title_provenance: str
    url: str
    source_published_at: str | None
    source_publication_raw: str | None
    publication_precision: Literal["timestamp", "date", "unknown"]
    publication_claim: str | None
    first_seen_at: str | None
    exact_tickers: tuple[str, ...]
    derived_tickers: tuple[str, ...]
    ticker_mapping_status: str
    model_annotations: dict[str, JsonValue]
    upstream_job_started_at: str | None
    quality_flags: tuple[str, ...]
    first_known_at: datetime | None = None
    packet_key: str | None = None


@dataclass(frozen=True)
class Parsed:
    items: tuple[Item, ...]
    returned_count: int
    limited: bool
    truncated: bool
    upstream_status: str | None


def _optional_text(value: object, limit: int = 4000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text field")
    return value or None


def _clock(value: object, now: datetime, flags: list[str], name: str) -> str | None:
    raw = _optional_text(value, 256)
    if raw is None:
        return None
    try:
        clock = aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        if clock > now:
            raise ValueError("future clock")
    except ValueError:
        flags.append(f"{name}_unknown")
        return None
    return clock.isoformat()


def _tickers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("invalid ticker mapping")
    if not all(isinstance(t, str) and re.fullmatch(r"[A-Z0-9.\-]{1,16}", t) for t in value):
        raise ValueError("invalid ticker mapping")
    return tuple(sorted(set(cast(list[str], value))))


def parse(raw: bytes, endpoint: Endpoint, now: datetime) -> Parsed:
    """JSON boundary: verify producer content hashes BEFORE normalizing claims."""
    now = aware(now)
    if len(raw) > settings().max_body_bytes:
        raise ValueError("body_limit")
    def invalid_constant(value: str) -> None:
        raise ValueError("nonfinite JSON")
    value: object = json.loads(raw, parse_constant=invalid_constant)
    canonical(value)  # Also rejects overflow-to-infinity in any unused JSON field.
    if not isinstance(value, dict):
        raise ValueError("envelope required")
    envelope = cast(dict[str, object], value)
    if (envelope.get("schema"), envelope.get("provider"), envelope.get("coverage")) != ("context_news_v1", endpoint.provider, endpoint.coverage) or envelope.get("supports_negative_evidence") is not False:
        raise ValueError("schema/provider/curated coverage mismatch")
    source_items = envelope.get("items")
    if not isinstance(source_items, list) or len(source_items) > 10000:
        raise ValueError("invalid items")
    count = envelope.get("returned_count")
    if type(count) is not int or count != len(source_items):
        raise ValueError("inconsistent returned_count")
    if "item_count" in envelope and envelope["item_count"] != count:
        raise ValueError("inconsistent item_count")
    for field in ("limited", "truncated"):
        if field in envelope and type(envelope[field]) is not bool:
            raise ValueError("invalid limit flag")
    items: list[Item] = []
    seen: set[tuple[str, str]] = set()
    for entry in source_items[:settings().max_items]:
        if not isinstance(entry, dict):
            raise ValueError("item object required")
        obj = cast(dict[str, object], entry)
        identity = _optional_text(obj.get("id"), 256)
        version = obj.get("content_version_hash")
        basis = {k: v for k, v in obj.items() if k != "content_version_hash" and (endpoint.provider == "mahalle" or k != "id")}
        if not identity or not isinstance(version, str) or version != digest(canonical(basis)):
            raise ValueError("content_version_hash mismatch")
        if (identity, version) in seen:
            continue
        seen.add((identity, version))
        if obj.get("supports_negative_evidence", False) is not False:
            raise ValueError("item cannot support negative evidence")
        flags: list[str] = []
        precision = obj.get("publication_precision")
        publication = _optional_text(obj.get("source_published_at"), 256)
        if precision == "timestamp":
            publication = _clock(publication, now, flags, "publication")
            if publication is None:
                precision = "unknown"
        elif precision == "date":
            try:
                if publication is None or date.fromisoformat(publication) > now.date():
                    raise ValueError("future date")
            except ValueError:
                publication, precision = None, "unknown"
                flags.append("publication_unknown")
        elif precision == "unknown":
            publication = None
        else:
            raise ValueError("invalid publication precision")
        annotations = obj.get("model_annotations", {})
        if not isinstance(annotations, dict) or len(canonical(annotations)) > 65536:
            raise ValueError("invalid model annotations")
        exact, derived = _tickers(obj.get("exact_tickers", [])), _tickers(obj.get("derived_tickers", []))
        if set(exact) & set(derived):
            raise ValueError("exact/derived overlap")
        mapping = _optional_text(obj.get("ticker_mapping_status"), 80) or "unknown"
        if exact and mapping != "upstream_explicit":
            raise ValueError("exact mapping lacks explicit upstream claim")
        first = _clock(obj.get("first_seen_at"), now, flags, "first_seen")
        if first is not None:
            flags.append("upstream_first_seen_unverified")
        title = _optional_text(obj.get("source_title"), 4000)
        provenance = _optional_text(obj.get("title_provenance"), 100)
        if endpoint.provider == "stajyer" and provenance != "upstream_publisher_metadata":
            if title:
                annotations = {**annotations, "unverified_catalog_title": title}
            title = None
            provenance = provenance or "unknown"
        items.append(Item(provider=endpoint.provider, id=identity, content_version_hash=version,
                          source_id=_optional_text(obj.get("source_id"), 256), source=_optional_text(obj.get("source"), 256),
                          source_title=title, title_provenance=provenance or "upstream_catalog_value",
                          url=article_url(obj.get("url")), source_published_at=publication,
                          source_publication_raw=_optional_text(obj.get("source_publication_raw"), 256),
                          publication_precision=precision,
                          publication_claim=_optional_text(obj.get("publication_claim"), 100), first_seen_at=first,
                          exact_tickers=exact, derived_tickers=derived, ticker_mapping_status=mapping,
                          model_annotations=cast(dict[str, JsonValue], annotations),
                          upstream_job_started_at=_clock(obj.get("upstream_job_started_at"), now, flags, "job_started"),
                          quality_flags=tuple(flags)))
    return Parsed(tuple(items), count, bool(envelope.get("limited", False)) or count > settings().max_items,
                  bool(envelope.get("truncated", False)) or count > settings().max_items,
                  _optional_text(envelope.get("status"), 80))


class FeedState(Frozen):
    provider: Provider
    status: str
    reason: str
    coverage: str
    upstream_status: str | None = None
    supports_negative_evidence: Literal[False] = False
    last_attempt_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    last_new_content_at: datetime | None = None
    packet_key: str | None = None
    body_hash: str | None = None
    returned_count: int | None = None
    limited: bool | None = None
    truncated: bool | None = None
    items: tuple[Item, ...] = ()


class Story(Frozen):
    canonical_url: str
    versions: tuple[Item, ...]
    independent_votes: Literal[False] = False


class SharedNewsView(Frozen):
    as_of: datetime
    enabled: bool
    role: Literal["context_only"] = "context_only"
    supports_negative_evidence: Literal[False] = False
    profile_hash: str
    feeds: tuple[FeedState, ...]
    stories: tuple[Story, ...]
