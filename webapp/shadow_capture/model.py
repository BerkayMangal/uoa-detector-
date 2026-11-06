"""Immutable JSON-record adapter boundary; payloads never imply a trusted quote."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

Kind = Literal['activation','candidate','target','attempt','quota_slot','packet',
               'attempt_done','outcome','halt','discovery']
KINDS: tuple[Kind, ...] = ('activation','candidate','target','attempt','quota_slot','packet',
                          'attempt_done','outcome','halt','discovery')


def aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('aware_clock_required')
    return at.astimezone(UTC)


def stored(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else aware(at)


def canonical(value: object) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str,allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Record:
    # Any is confined to this immutable database/wire JSON adapter boundary.
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
