"""Immutable timed reference DTOs. No execution, cost-adjusted P&L or news votes."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

Leg = Literal["own", "spy"]


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware physical clock required")
    return value.astimezone(UTC)


def stored(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else aware(value)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def clocks_are_aware(cls, value: object) -> object:
        return aware(value) if isinstance(value, datetime) else value


class Activation(Frozen):
    key: str
    profile_hash: str
    opening_profile_hash: str
    activated_at: datetime


class Target(Frozen):
    key: str
    activation_key: str
    profile_hash: str
    opening_profile_hash: str
    primary_key: str
    decision_key: str
    ticker: str
    day: date
    direction: Literal["up", "down"]
    instrument: str
    published_at: datetime
    entry_reference: float | None
    entry_source_at: datetime | None
    spy_entry: float | None
    spy_entry_source_at: datetime | None
    source_scan_id: str
    source_snapshot_hash: str
    source_card_hash: str
    horizon: str
    horizon_minutes: int
    target_at: datetime
    deadline: datetime
    eligible: bool
    reason: str
    discovered_at: datetime

    @property
    def call_key(self) -> str:
        return self.decision_key

    @property
    def call_at(self) -> datetime:
        return self.published_at


class Attempt(Frozen):
    key: str
    target_key: str
    ordinal: int
    claimed_at: datetime


class LegInput(Frozen):
    leg: Leg
    ticker: str
    status: str
    reason: str = ""
    requested_at: datetime | None = None
    received_at: datetime | None = None
    http_status: int | None = None
    payload: dict[str, JsonValue] | None = None
    raw_body: bytes | None = None
    wire_body_hash: str | None = None
    body_bytes: int = 0
    wire_attempted: bool = False
    native_daily_count: int | None = None
    native_count_at: datetime | None = None
    price: float | None = None
    trade_at: datetime | None = None
    market_phase: str | None = None
    source_path: str = ""


class LegEvidence(Frozen):
    key: str
    attempt_key: str
    leg: Leg
    ticker: str
    status: str
    reason: str
    requested_at: datetime | None
    received_at: datetime | None
    receipt_at: datetime
    confirmed_at: datetime
    price: float | None
    trade_at: datetime | None
    http_status: int | None
    payload_hash: str
    wire_body_hash: str | None
    wire_attempted: bool
    native_daily_count: int | None = None
    native_count_at: datetime | None = None
    market_phase: str | None = None
    source_path: str = ""


class Completion(Frozen):
    key: str
    attempt_key: str
    target_key: str
    ordinal: int
    completed_at: datetime
    confirmed_at: datetime
    own_key: str | None
    spy_key: str | None
    reason: str
    own_confirmed_at: datetime | None = None
    spy_confirmed_at: datetime | None = None


class Assessment(Frozen):
    key: str
    target_key: str
    assessed_at: datetime
    status: str
    reason: str
    own_key: str | None = None
    spy_key: str | None = None
    return_pct: float | None = None
    excess_pp: float | None = None


class Reservation(Frozen):
    granted: bool
    reason: str
    slot_key: str | None = None
    module_reserved: int = 0
    shared_reserved: int | None = None


class Halt(Frozen):
    day: date
    reason: str
    at: datetime
    until: datetime | None = None


class OpenAttempts(Frozen):
    rows: tuple[tuple[Attempt, Target], ...]
    limited: bool
