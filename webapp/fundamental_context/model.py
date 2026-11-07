"""Typed financial context, immutable clocks and explicit metric missingness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

Endpoint = Literal["fundamental-breakdown", "financials", "earnings"]
Kind = Literal["activation", "claim", "packet", "halt"]
KINDS: tuple[Kind, ...] = ("activation", "claim", "packet", "halt")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_clock_required")
    return value.astimezone(UTC)


def stored(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else aware(value)


@dataclass(frozen=True)
class Metric:
    name: str
    value: float | None
    unit: str
    reason: str
    source_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Filing:
    accession: str | None
    filing_date: date | None
    period_end: date | None
    report_type: str | None
    currency: str | None
    vendor_inserted_at: datetime | None = None
    vendor_updated_at: datetime | None = None
    report_date: date | None = None
    report_time: str | None = None
    accounting_basis: str | None = None


@dataclass(frozen=True)
class Facts:
    ticker: str
    endpoint: Endpoint
    available_at: datetime
    metrics: tuple[Metric, ...]
    filings: tuple[Filing, ...]
    missing: tuple[str, ...]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Record:
    # Any is confined to immutable wire/database JSON adapter boundaries.
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
