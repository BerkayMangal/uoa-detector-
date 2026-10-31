"""Strict data adapter. Reserved Study G is never a research input."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from webapp.calibration.model import Bar
from webapp.call_research.settings import ROOT, Settings, settings


@dataclass(frozen=True)
class Dataset:
    bars: tuple[Bar, ...]
    days: tuple[date, ...]
    stocks: tuple[str, ...]
    source_hash: str
    missing_context: tuple[str, ...]


def prepare(bars: tuple[Bar, ...], source_hash: str = "", cfg: Settings | None = None) -> Dataset:
    s = cfg or settings()
    seen: set[tuple[str, date]] = set()
    for b in bars:
        if (b.ticker, b.day) in seen:
            raise ValueError("duplicate price session")
        seen.add((b.ticker, b.day))
    names = {b.ticker for b in bars}
    return Dataset(
        bars,
        tuple(sorted({b.day for b in bars if b.ticker == s.benchmarks[0]})),
        tuple(sorted(names - set(s.reference_only))),
        source_hash,
        tuple(t for t in s.context_tickers if t not in names),
    )


def load(path: Path | None = None, expected_sha: str | None = None) -> Dataset:
    s = settings()
    source = path or ROOT / s.source_path
    if "study_g" in source.resolve().parts:
        raise ValueError("reserved prospective data is forbidden")
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != (expected_sha or s.source_sha256):
        raise ValueError("research source checksum mismatch")
    bars = tuple(
        Bar(
            r["ticker"],
            date.fromisoformat(r["day"]),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r["volume"]) if r.get("volume") else None,
        )
        for r in csv.DictReader(raw.decode().splitlines())
    )
    if not all(
        b.valid and b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in bars
    ):
        raise ValueError("invalid research OHLC")
    result = prepare(bars, checksum)
    if not all(t in {b.ticker for b in bars} for t in s.benchmarks):
        raise ValueError("missing reference universe")
    return result
