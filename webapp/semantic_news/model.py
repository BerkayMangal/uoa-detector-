"""Typed immutable wire/record boundaries for source-bound model interpretations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

Kind = Literal["claim", "response", "binding", "halt", "skip"]
KINDS: tuple[Kind, ...] = ("claim", "response", "binding", "halt", "skip")
Direction = Literal["positive", "negative", "mixed", "neutral", "unknown"]
_HEX = re.compile(r"[a-f0-9]{64}")
_TICKER = re.compile(r"[A-Z][A-Z0-9.-]{0,15}")


def aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware_clock_required")
    return at.astimezone(UTC)


def stored(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else aware(at)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Record:
    # Any is confined to this immutable, checksummed JSON persistence boundary.
    key: str
    kind: Kind
    at: datetime
    day: date | None
    parent_key: str | None
    unique_owner: str | None
    payload: dict[str, Any]
    checksum: str
    confirmed_at: datetime | None
    blob: bytes | None = None


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Headline(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    headline: str = Field(min_length=1, max_length=1600)
    source: str = Field(min_length=1, max_length=256)
    published_at: str | None


class Request(StrictModel):
    version: Literal["semantic_news_v1"] = "semantic_news_v1"
    request_id: str
    ticker: str
    source_hash: str
    items: tuple[Headline, ...] = Field(min_length=1, max_length=3)

    @field_validator("request_id", "source_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _HEX.fullmatch(value):
            raise ValueError("invalid_hash")
        return value

    @field_validator("ticker")
    @classmethod
    def valid_ticker(cls, value: str) -> str:
        if not _TICKER.fullmatch(value):
            raise ValueError("invalid_ticker")
        return value

    def core(self) -> dict[str, object]:
        return {
            "version": self.version,
            "ticker": self.ticker,
            "source_hash": self.source_hash,
            "items": [item.model_dump() for item in self.items],
        }

    def valid(self, now: datetime) -> bool:
        if self.request_id != digest(self.core()) or len({i.id for i in self.items}) != len(
            self.items
        ):
            return False
        for item in self.items:
            if item.published_at is not None:
                try:
                    if aware(
                        datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
                    ) > aware(now):
                        return False
                except ValueError:
                    return False
        return True


class Evidence(StrictModel):
    quote: str = Field(min_length=1, max_length=1600)
    field: Literal["direction", "event", "certainty", "metric"]


class Interpretation(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    ticker: str
    relation: Literal["direct", "indirect", "unknown"]
    event: Literal[
        "earnings",
        "guidance",
        "merger",
        "capital",
        "regulatory",
        "analyst",
        "product",
        "macro",
        "other",
    ]
    direction: Direction
    certainty: Literal["confirmed", "expected", "rumor", "denied", "unknown"]
    materiality: Literal["high", "medium", "low", "unknown"]
    reason: str = Field(max_length=600)
    evidence: tuple[Evidence, ...] = Field(max_length=12)
    limitations: tuple[str, ...] = Field(max_length=12)

    @field_validator("limitations")
    @classmethod
    def bounded_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(item) > 600 for item in value):
            raise ValueError("limitation_too_long")
        return value


class Usage(StrictModel):
    input_tokens: StrictInt | None = Field(ge=0, le=65536)
    output_tokens: StrictInt | None = Field(ge=0, le=2048)
    cost_usd: float | None = Field(ge=0, allow_inf_nan=False)

    @field_validator("cost_usd", mode="before")
    @classmethod
    def strict_cost(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError("numeric_cost_required")
        return value


class Envelope(StrictModel):
    version: Literal["semantic_news_v1"]
    input_hash: str
    model: str = Field(min_length=1, max_length=200)
    prompt_hash: str
    schema_hash: str
    status: Literal["completed", "pending", "unknown", "rate_limited", "disabled"]
    analysis_started_at: datetime | None
    analysis_finished_at: datetime | None
    recorded_at: datetime | None
    items: tuple[Interpretation, ...] = Field(max_length=3)
    usage: Usage

    @field_validator("input_hash", "prompt_hash", "schema_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _HEX.fullmatch(value):
            raise ValueError("invalid_hash")
        return value

    @field_validator("analysis_started_at", "analysis_finished_at", "recorded_at", mode="before")
    @classmethod
    def explicit_clock(cls, value: object) -> object:
        if value is not None and not isinstance(value, str | datetime):
            raise ValueError("explicit_iso_clock_required")
        return value

    @field_validator("analysis_started_at", "analysis_finished_at", "recorded_at")
    @classmethod
    def valid_clock(cls, value: datetime | None) -> datetime | None:
        return aware(value) if value is not None else None


class Metadata(StrictModel):
    version: Literal["semantic_news_v1"]
    model: str = Field(min_length=1, max_length=200)
    prompt_hash: str
    schema_hash: str
    configured: StrictBool
    max_attempts: Literal[8]
    max_input_bytes: Literal[16384]
    max_items: Literal[3]
    max_headline_characters: Literal[1600]
    max_output_tokens: Literal[2048]
    timeout_seconds: Literal[45]

    @field_validator("prompt_hash", "schema_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _HEX.fullmatch(value):
            raise ValueError("invalid_hash")
        return value

    def binding(self) -> dict[str, str]:
        return {
            "version": self.version,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "schema_hash": self.schema_hash,
        }


@dataclass(frozen=True)
class Source:
    candidate_key: str
    candidate_checksum: str
    ticker: str
    day: date
    candidate_at: datetime
    entry_target: datetime
    source_packet_key: str
    source_payload_hash: str
    source_available_at: datetime
    request: Request
    source_headline_count: int = 0


@dataclass(frozen=True)
class EmptySource:
    candidate_key: str
    candidate_checksum: str
    ticker: str
    day: date
    entry_target: datetime
    source_packet_key: str
    source_payload_hash: str
    source_available_at: datetime
    reason: str
