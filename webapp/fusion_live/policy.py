"""Registered forward truth table; neither prices nor outcomes enter this function."""

from __future__ import annotations

from datetime import datetime

from webapp.fusion_live.model import Arm, FundamentalEvidence, NewsEvidence, Verdict
from webapp.shadow_capture.model import aware


def _one(
    value: bool | None,
    status: str,
    available: datetime | None,
    reason: str,
    now: datetime,
    cutoff: datetime,
) -> Verdict:
    if status != "known":
        return Verdict(
            "PENDING" if status == "pending" and value is None else "PAPER_UNKNOWN", (reason,)
        )
    if available is not None and (aware(available) > now or available > cutoff):
        return Verdict("PAPER_UNKNOWN", ("Kanıt, giriş öncesi karar süresine yetişmedi.",))
    if value is not None and available is not None:
        return Verdict("PAPER_ACCEPT" if value else "PAPER_SKIP", (reason,), (available,))
    if status == "pending":
        return Verdict("PENDING", (reason,))
    return Verdict("PAPER_UNKNOWN", (reason,))


def evaluate(
    arm: Arm, now: datetime, cutoff: datetime, news: NewsEvidence, fundamental: FundamentalEvidence
) -> Verdict:
    now, cutoff = aware(now), aware(cutoff)
    if now >= cutoff:
        return Verdict("PAPER_UNKNOWN", ("Girişten en az beş saniye önce doğrulanmış karar yok.",))
    if arm == "base":
        return Verdict("PAPER_ACCEPT", ("Özgün taban akış/fiyat koşulları doğrulandı.",))
    positive = _one(news.positive, news.status, news.available_at, news.reason, now, cutoff)
    if arm == "semantic_positive":
        return positive
    if arm == "semantic_veto":
        return _one(news.veto_accept, news.status, news.available_at, news.reason, now, cutoff)
    if arm != "semantic_fundamental":
        raise ValueError("unknown_fusion_arm")
    quality = _one(
        fundamental.quality,
        fundamental.status,
        fundamental.available_at,
        fundamental.reason,
        now,
        cutoff,
    )
    # Known failed conjuncts are a definite skip; unknown never becomes support.
    failed = tuple(v for v in (positive, quality) if v.state == "PAPER_SKIP")
    if failed:
        return Verdict(
            "PAPER_SKIP",
            tuple(r for v in failed for r in v.reasons),
            tuple(t for v in failed for t in v.used_at),
        )
    if positive.state == quality.state == "PAPER_ACCEPT":
        return Verdict(
            "PAPER_ACCEPT", positive.reasons + quality.reasons, positive.used_at + quality.used_at
        )
    unknown = tuple(v for v in (positive, quality) if v.state == "PAPER_UNKNOWN")
    if unknown:
        return Verdict(
            "PAPER_UNKNOWN",
            tuple(r for v in unknown for r in v.reasons),
            tuple(t for v in unknown for t in v.used_at),
        )
    return Verdict(
        "PENDING", tuple(r for v in (positive, quality) if v.state == "PENDING" for r in v.reasons)
    )
