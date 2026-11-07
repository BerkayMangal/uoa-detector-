"""Typed availability-aware evidence, distinct from a trading probability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

State = Literal["PENDING", "PAPER_ACCEPT", "PAPER_SKIP", "PAPER_UNKNOWN"]
Arm = Literal["base", "semantic_positive", "semantic_veto", "semantic_fundamental"]


@dataclass(frozen=True)
class NewsEvidence:
    status: str = "pending"
    positive: bool | None = None
    veto_accept: bool | None = None
    available_at: datetime | None = None
    reason: str = "Haber yorumu bekleniyor."
    model: str | None = None
    prompt_hash: str | None = None
    schema_hash: str | None = None
    source_hash: str | None = None
    direction: str | None = None
    input_count: int = 0


@dataclass(frozen=True)
class FundamentalEvidence:
    status: str = "pending"
    quality: bool | None = None
    available_at: datetime | None = None
    reason: str = "Aynı dönem ve para birimindeki finansallar bekleniyor."
    revenue_yoy_pct: float | None = None
    operating_margin_pct: float | None = None
    source_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    state: State
    reasons: tuple[str, ...]
    used_at: tuple[datetime, ...] = ()
