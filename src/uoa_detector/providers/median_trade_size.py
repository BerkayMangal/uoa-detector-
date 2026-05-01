"""``MedianTradeSizeProvider`` Protocol — feeds Module 37 (Relative Premium).

Module 37 scores how much a print stands out vs the ticker's typical trade
size. The denominator — the ticker's 30-day median trade size in USD
premium — is reference data: slow-changing, ticker-specific, expensive to
re-derive at scoring time. The provider abstracts the source.

Two implementations ship in Phase 2:
  - ``InMemoryMedianTradeSizeProvider``: takes a ``dict[str, Decimal]`` at
    construction. Used by tests and the synthetic CLI scenario.
  - ``CSVMedianTradeSizeProvider``: reads ``data/medians.csv``. Used by the
    --source synthetic --scenario default CLI mode for parity with what
    Phase 3 will plug in.
  - ``NoOpMedianTradeSizeProvider``: always returns ``None``. Stages that
    receive ``None`` set ``relative_premium_score = None`` and the labeler
    handles None gracefully per the Phase 2 prompt's instruction.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class MedianTradeSizeProvider(Protocol):
    """Source of per-ticker median trade size in USD premium.

    The ``window`` argument is the trailing window in days; Phase 2's
    Module 37 calls ``median_premium("AAPL", 30)`` for the spec-defined
    30-day window. Phase 3 implementations may cache aggressively since
    these values change slowly.
    """

    async def median_premium(self, ticker: str, window_days: int) -> Decimal | None:
        """Return median premium in USD over the trailing ``window_days``.

        Returns ``None`` when no data is available for ``ticker``.
        """
        ...


class InMemoryMedianTradeSizeProvider:
    """Test/CLI implementation backed by a ``dict[ticker, Decimal]``.

    Window is ignored — the dict is presumed to already represent the
    desired window. This is fine for tests; Phase 3's real providers will
    parameterise on window properly.
    """

    def __init__(self, medians: dict[str, Decimal]) -> None:
        self._medians = dict(medians)

    async def median_premium(self, ticker: str, window_days: int) -> Decimal | None:
        del window_days  # ignored for in-memory
        return self._medians.get(ticker)


class CSVMedianTradeSizeProvider:
    """Loader for ``data/medians.csv`` with a 5-ticker stub baseline.

    Expected schema:
        ticker,median_premium_usd_30d
        AAPL,4500.00
        ...

    Read once at construction. Window argument is ignored as with the
    in-memory provider — the CSV is one window.
    """

    def __init__(self, csv_path: Path) -> None:
        self._csv_path = csv_path
        self._cache: dict[str, Decimal] = {}
        self._loaded = False

    def _load(self) -> None:
        with self._csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                self._cache[row["ticker"]] = Decimal(row["median_premium_usd_30d"])
        self._loaded = True

    async def median_premium(self, ticker: str, window_days: int) -> Decimal | None:
        del window_days
        if not self._loaded:
            self._load()
        return self._cache.get(ticker)


class NoOpMedianTradeSizeProvider:
    """Always returns ``None``. Used as the default when no provider is wired,
    so Module 37 produces ``relative_premium_score = None`` rather than
    crashing.
    """

    async def median_premium(self, ticker: str, window_days: int) -> Decimal | None:
        del ticker, window_days
        return None
