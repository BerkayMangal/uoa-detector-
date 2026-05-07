"""Tier-N universe CSV reader.

Phase 3.3.4.1: parses the universe files at
``data/universes/tier{1,2}_*.csv`` into validated rows, dropping
malformed lines with structured warnings rather than crashing the
whole download.

Schema (header line, 5 columns):

  ticker, sector, market_cap_usd_b_approx,
  options_volume_30d_avg_approx, notes

Sample row:
  PLTR,tech_software,~50,~250000,large but high gamma activity

The numeric columns use the operator-friendly ``~50`` notation
('approximately 50') because the universe files are curated by a
human who's eyeballing rough magnitudes, not pulling exact figures.
We strip the ``~`` and parse to int (returning None on failure).

decision (Pydantic strict model, validator drops bad rows):
  Each row is validated through ``UniverseEntry``. Malformed rows
  log a warning and are skipped — a bad row in the universe file
  shouldn't abort the entire download. The acceptance doc names
  'tier-2 starter (51 tickers)' so missing one or two due to data
  quality is acceptable; missing the whole batch is not.

decision (no normalisation of sector strings):
  The sector column is free-form (``tech_software``,
  ``fintech_lending``, etc.). We don't normalise it because Phase
  3.4's M25 (Sector Confirmation) gets sector mappings from
  ``SectorMapProvider`` (UW or another source), not from this
  CSV. The CSV is a curation artefact, not a data source.

decision (return tuple, not generator):
  Universe files are small (~50-100 rows). A tuple is easier to
  iterate twice (once for validation, once for orchestration)
  and avoids generator-exhaustion bugs in callers.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    pass


_logger = logging.getLogger(__name__)
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class UniverseEntry(BaseModel):
    """One ticker in a tier universe.

    Numeric columns are optional because the operator may leave
    them blank for new tickers under review.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    sector: str
    market_cap_usd_b_approx: int | None = None
    options_volume_30d_avg_approx: int | None = None
    notes: str = ""

    @field_validator("ticker")
    @classmethod
    def _normalise_ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not _TICKER_RE.match(v):
            msg = f"invalid ticker: {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("sector")
    @classmethod
    def _strip_sector(cls, v: str) -> str:
        v = v.strip()
        if not v:
            msg = "sector cannot be empty"
            raise ValueError(msg)
        return v


def _parse_approx_int(raw: str) -> int | None:
    """Parse an operator-friendly '~50' / '~250000' / '' value."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("~"):
        raw = raw[1:].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def read_universe(path: Path) -> tuple[UniverseEntry, ...]:
    """Read a universe CSV and return validated entries.

    Malformed rows are logged and skipped. Header line is required.
    Returns an empty tuple if the file is missing or has no valid rows.
    """
    if not path.exists():
        msg = f"universe file not found: {path}"
        raise FileNotFoundError(msg)

    entries: list[UniverseEntry] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            _logger.warning("empty universe file: %s", path)
            return ()
        required = {"ticker", "sector"}
        missing = required - set(reader.fieldnames)
        if missing:
            msg = (
                f"universe file {path} missing required columns: "
                f"{sorted(missing)}"
            )
            raise ValueError(msg)
        for line_no, row in enumerate(reader, start=2):
            try:
                entry = UniverseEntry(
                    ticker=row.get("ticker", ""),
                    sector=row.get("sector", ""),
                    market_cap_usd_b_approx=_parse_approx_int(
                        row.get("market_cap_usd_b_approx", ""),
                    ),
                    options_volume_30d_avg_approx=_parse_approx_int(
                        row.get("options_volume_30d_avg_approx", ""),
                    ),
                    notes=(row.get("notes") or "").strip(),
                )
                entries.append(entry)
            except (ValueError, TypeError) as exc:
                _logger.warning(
                    "skipping malformed universe row at %s line %d: %s",
                    path, line_no, exc,
                )
                continue
    # De-dup on ticker (keep first); operator-typo defence.
    seen: set[str] = set()
    deduped: list[UniverseEntry] = []
    for e in entries:
        if e.ticker in seen:
            _logger.warning(
                "duplicate ticker in %s: %s — keeping first occurrence",
                path, e.ticker,
            )
            continue
        seen.add(e.ticker)
        deduped.append(e)
    return tuple(deduped)


def tickers_only(entries: tuple[UniverseEntry, ...]) -> tuple[str, ...]:
    """Convenience: extract just the ticker column, preserving order."""
    return tuple(e.ticker for e in entries)
