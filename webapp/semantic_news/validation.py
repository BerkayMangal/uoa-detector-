"""Ground model claims in the same input item; never infer a profit probability."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from webapp.semantic_news.model import Direction, Envelope, Metadata, Request, aware

_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:%|[bmkBMK])?")
_PRICE_CLAIM = re.compile(r"priced[ -]in|fiyatlanm[ıi][şs]", re.I)


@dataclass(frozen=True)
class Assessment:
    direction: Direction
    supports: bool | None
    reason: str


def decode(raw: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise ValueError("nonfinite_json")

    value: object = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    return value


def validate(
    request: Request, response: Envelope, metadata: Metadata, received_at: datetime
) -> Assessment:
    at = aware(received_at)
    if not request.valid(at) or response.input_hash != request.request_id:
        raise ValueError("input_binding_mismatch")
    if any(getattr(response, k) != v for k, v in metadata.binding().items()):
        raise ValueError("model_binding_mismatch")
    clocks = (response.analysis_started_at, response.analysis_finished_at, response.recorded_at)
    if response.status != "completed":
        if response.items:
            raise ValueError("partial_result_not_completed")
        if any(t is not None and t > at for t in clocks):
            raise ValueError("future_model_clock")
        return Assessment("unknown", None, response.status)
    if any(t is None for t in clocks):
        raise ValueError("missing_model_clock")
    start, finish, recorded = clocks
    assert start is not None and finish is not None and recorded is not None
    if not start <= finish <= recorded <= at:
        raise ValueError("invalid_model_chronology")
    inputs = {i.id: i for i in request.items}
    if len(response.items) != len(inputs) or {i.id for i in response.items} != set(inputs):
        raise ValueError("item_binding_mismatch")
    if len({i.id for i in response.items}) != len(response.items):
        raise ValueError("duplicate_output_id")
    for item in response.items:
        original = inputs[item.id]
        if item.ticker != request.ticker:
            raise ValueError("ticker_mismatch")
        if any(e.quote not in original.headline for e in item.evidence):
            raise ValueError("invented_or_cross_item_span")
        if item.direction != "unknown" and not any(e.field == "direction" for e in item.evidence):
            raise ValueError("direction_without_grounding")
        if item.event != "other" and not any(e.field == "event" for e in item.evidence):
            raise ValueError("event_without_grounding")
        if item.certainty != "unknown" and not any(e.field == "certainty" for e in item.evidence):
            raise ValueError("certainty_without_grounding")
        # A valid quote does not license an invented number elsewhere in the
        # reason. Match exact number spellings to this headline only.
        if any(
            n not in set(_NUMBER.findall(original.headline))
            for text in (item.reason, *item.limitations)
            for n in _NUMBER.findall(text)
        ):
            raise ValueError("invented_reason_number")
        if _PRICE_CLAIM.search(item.reason):
            raise ValueError("text_only_price_incorporation_claim")
    direct = [i for i in response.items if i.relation == "direct"]
    if len(direct) != len(response.items):
        return Assessment("unknown", None, "indirect_or_unclassified_coverage")
    if not direct:
        return Assessment("unknown", None, "no_direct_issuer_evidence")
    directions = {i.direction for i in direct}
    if "mixed" in directions or {"positive", "negative"} <= directions:
        return Assessment("mixed", False, "conflicting_direct_headlines")
    if any(i.certainty != "confirmed" or i.direction == "unknown" for i in direct):
        return Assessment("unknown", None, "speculative_denied_or_unknown")
    if "negative" in directions:
        return Assessment("negative", False, "confirmed_direct_negative")
    if "positive" in directions:
        return Assessment("positive", True, "confirmed_direct_positive")
    return Assessment("neutral", False, "confirmed_direct_neutral")
