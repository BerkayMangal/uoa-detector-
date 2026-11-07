"""Defensive read-model adapters; model prose is never converted to a numeric fact.

Any only enters through dataclass-to-JSON presentation boundaries. The output is
explicitly typed evidence; no unknown scalar becomes zero or a positive vote.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from webapp.fusion_live import config
from webapp.fusion_live.model import FundamentalEvidence, NewsEvidence
from webapp.shadow_capture.model import aware


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) and all(isinstance(k, str) for k in value) else {}


def sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list | tuple) else ()


def _time(value: object) -> datetime | None:
    try:
        return (
            aware(value)
            if isinstance(value, datetime)
            else aware(datetime.fromisoformat(value))
            if isinstance(value, str)
            else None
        )
    except (ValueError, TypeError):
        return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    return (
        float(value)
        if not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)
        else None
    )


def semantic(row: object, *, candidate_key: str, ticker: str, now: datetime) -> NewsEvidence:
    data = mapping(row)
    if not data:
        return NewsEvidence()
    if data.get("candidate_key") != candidate_key or data.get("ticker") != ticker:
        return NewsEvidence(status="unknown", reason="Yorum ile özgün aday kimliği eşleşmiyor.")
    status = str(data.get("status", "unknown"))
    if status in ("pending", "collecting", "claimed", "awaiting"):
        return NewsEvidence()
    at = _time(data.get("available_at"))
    fields: dict[str, Any] = dict(
        available_at=at,
        reason=str(data.get("reason") or "Yorum yönü kesinleşmedi."),
        model=_str(data.get("model")),
        prompt_hash=_str(data.get("prompt_hash")),
        schema_hash=_str(data.get("schema_hash")),
        source_hash=_str(data.get("source_hash")),
        direction=_str(data.get("direction")),
        input_count=data.get("input_count") if type(data.get("input_count")) is int else 0,
    )
    source_hash = data.get("source_hash")
    # A verified checked-empty row has no model request/source_hash. Its public
    # view instead binds the original physically receipted empty UW packet.
    # Only this explicit zero-item path may use that packet's payload digest;
    # missing model provenance on nonempty interpretations must remain unknown.
    if source_hash is None and data.get("checked_empty") is True and fields["input_count"] == 0:
        payload_hash, packet_key = data.get("source_payload_hash"), data.get("source_packet_key")
        if all(
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
            for value in (payload_hash, packet_key)
        ):
            source_hash = payload_hash
            fields["source_hash"] = payload_hash
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(c not in "0123456789abcdef" for c in source_hash)
    ):
        return NewsEvidence(status="unknown", reason="Yorumun kaynak kimliği doğrulanamadı.")
    if at is None or at > aware(now) or data.get("eligible_before_entry") is not True:
        return NewsEvidence(status="unknown", **fields)
    if data.get("checked_empty") is True and fields["input_count"] == 0:
        if status not in ("skipped", "completed", "ready", "known", "ok"):
            return NewsEvidence(status="unknown", **fields)
        return NewsEvidence(status="known", positive=False, veto_accept=True, **fields)
    if status not in ("completed", "ready", "known", "ok"):
        return NewsEvidence(status="unknown", **fields)
    items = [mapping(i) for i in sequence(data.get("items"))]
    direct_confirmed = bool(items) and all(
        i.get("ticker") == ticker
        and i.get("relation") == "direct"
        and i.get("certainty") == "confirmed"
        for i in items
    )
    if not direct_confirmed:
        return NewsEvidence(status="unknown", **fields)
    direction = fields["direction"]
    if direction in ("negative", "mixed"):
        return NewsEvidence(status="known", positive=False, veto_accept=False, **fields)
    if direction == "positive" and data.get("supports") is True:
        return NewsEvidence(status="known", positive=True, veto_accept=True, **fields)
    if direction == "neutral":
        return NewsEvidence(status="known", positive=False, veto_accept=True, **fields)
    return NewsEvidence(status="unknown", **fields)


def fundamental(
    row: object, *, candidate_key: str, ticker: str, now: datetime
) -> FundamentalEvidence:
    data = mapping(row)
    if not data:
        return FundamentalEvidence()
    if data.get("candidate_key") != candidate_key or data.get("ticker") != ticker:
        return FundamentalEvidence(
            status="unknown", reason="Finansal kaynak ile aday kimliği eşleşmiyor."
        )
    observations = [mapping(o) for o in sequence(data.get("observations"))]
    source = next((o for o in observations if o.get("endpoint") == "financials"), None)
    if source is None:
        return (
            FundamentalEvidence()
            if str(data.get("status")) in ("pending", "collecting", "claimed", "awaiting")
            else FundamentalEvidence(
                status="unknown", reason="Finansal tablo gözlemi doğrulanamadı."
            )
        )
    available_at = _time(source.get("available_at"))
    if str(source.get("status")) in ("pending", "collecting", "claimed", "awaiting"):
        return FundamentalEvidence()
    if source.get("status") != "recorded":
        return FundamentalEvidence(
            status="unknown", reason="Finansal kaynak başarılı ve doğrulanmış değil."
        )
    if available_at is None or available_at > aware(now):
        return FundamentalEvidence(
            status="unknown", reason="Finansal tablonun fiziksel kullanılabilirliği doğrulanamadı."
        )
    raw_hash = source.get("body_sha256")
    if (
        not isinstance(raw_hash, str)
        or len(raw_hash) != 64
        or any(c not in "0123456789abcdef" for c in raw_hash)
    ):
        return FundamentalEvidence(
            status="unknown", reason="Finansal paket kaynak özeti doğrulanamadı."
        )
    facts = mapping(source.get("facts"))
    if (
        facts.get("ticker") != ticker
        or facts.get("endpoint") != "financials"
        or _time(facts.get("available_at")) != available_at
    ):
        return FundamentalEvidence(
            status="unknown",
            reason="Finansal metrik kaynağı veya kullanılabilirlik saati eşleşmiyor.",
        )
    metrics = {
        str(m.get("name")): _number(m.get("value"))
        for item in sequence(facts.get("metrics"))
        if (m := mapping(item)) and m.get("unit") == "percent" and sequence(m.get("source_paths"))
    }
    rev, margin = metrics.get("revenue_yoy_pct"), metrics.get("operating_margin_pct")
    hashes = (_str(source.get("body_sha256")),)
    fields: dict[str, Any] = dict(
        available_at=available_at,
        revenue_yoy_pct=rev,
        operating_margin_pct=margin,
        source_hashes=tuple(h for h in hashes if h is not None),
    )
    failed = (rev is not None and rev < config.settings().fundamental_revenue_yoy_min_pct) or (
        margin is not None and margin <= config.settings().fundamental_operating_margin_min_pct
    )
    if not failed and (rev is None or margin is None):
        return FundamentalEvidence(
            status="unknown",
            reason="Karşılaştırılabilir gelir büyümesi veya faaliyet marjı bilinmiyor.",
            **fields,
        )
    rev_text = f"%{rev:+.2f}" if rev is not None else "bilinmiyor"
    margin_text = f"%{margin:+.2f}" if margin is not None else "bilinmiyor"
    return FundamentalEvidence(
        status="known",
        quality=not failed,
        reason=f"Gelir yıllık değişimi {rev_text}; faaliyet marjı {margin_text}.",
        **fields,
    )
