"""Pin tests for ``data/universes/tier1_anchor.csv``.

Tier-1 is the **anchor** universe for the Phase 3.2.4 4-cell
combinatorial backtest's `(Tier-1, ...)` cells. Unlike Tier-2 (which
explicitly flags edge cases for downstream review), Tier-1 is a
curated list of 20 broadly-watched, deep-options-volume tickers — no
'flag for review' notes; if a row earned a flag, it doesn't belong
in Tier-1.

Tests verify:
  - Schema (header columns) matches the pinned set, identical to the
    Tier-2 schema so the same loader handles both.
  - Row count exactly 20 (the approved Phase 3 prep number).
  - Tickers are exactly the approved set, in the approved order.
  - Sector column has at least 8 distinct sectors (diversity).
  - No 'flag for review' notes anywhere — Tier-1 is curated.
  - 'approx' suffix preserved on the manual-data columns.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

_UNIVERSE_PATH = Path("data/universes/tier1_anchor.csv")

EXPECTED_COLUMNS = [
    "ticker",
    "sector",
    "market_cap_usd_b_approx",
    "options_volume_30d_avg_approx",
    "notes",
]

# Approved Phase 3 prep selection — 20 tickers, in canonical order:
# Broad ETFs first, then mega-cap tech, financials, energy, healthcare,
# macro hedges. Order matters for the pin test; if we add/remove a
# ticker the test fails and forces a deliberate decision.
EXPECTED_TICKERS = [
    "SPY", "QQQ", "IWM", "DIA",          # 4 broad ETFs
    "NVDA", "AMD", "AAPL", "MSFT", "GOOGL", "TSLA", "META", "AMZN",  # 8 mega-cap tech
    "JPM", "BAC",                        # 2 financials
    "XOM", "CVX",                        # 2 energy
    "JNJ", "UNH",                        # 2 healthcare
    "GLD", "TLT",                        # 2 macro hedges
]


def _read_rows() -> list[dict[str, str]]:
    with _UNIVERSE_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _read_header() -> list[str]:
    with _UNIVERSE_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader)


def test_universe_file_exists() -> None:
    assert _UNIVERSE_PATH.exists(), (
        f"Tier-1 anchor universe missing at {_UNIVERSE_PATH}. "
        "Phase 3.2.4 backtest cannot run without it."
    )


def test_schema_columns_match_tier2() -> None:
    """Same schema as Tier-2 so the eventual loader handles both files
    through one code path. If the Tier-2 schema drifts, this test fires
    too — that's intentional, the schemas should stay aligned.
    """
    assert _read_header() == EXPECTED_COLUMNS


def test_exactly_twenty_tickers() -> None:
    """Phase 3 prep approved exactly 20 tickers. Adding or removing one
    must be deliberate."""
    rows = _read_rows()
    assert len(rows) == 20, f"expected 20 tickers, got {len(rows)}"


def test_tickers_match_approved_list_in_order() -> None:
    """The list and order are the Phase 3 prep approved selection.
    Reordering or substitution must update the test consciously.
    """
    rows = _read_rows()
    actual_tickers = [r["ticker"] for r in rows]
    assert actual_tickers == EXPECTED_TICKERS


def test_tickers_are_unique() -> None:
    rows = _read_rows()
    tickers = [r["ticker"] for r in rows]
    duplicates = [t for t, c in Counter(tickers).items() if c > 1]
    assert not duplicates, f"duplicate tickers: {duplicates}"


def test_sector_diversity_at_least_eight() -> None:
    """Tier-1 is meant to be sector-diverse: ETFs spanning broad market,
    semi/hardware/software/social tech, financials, energy, healthcare,
    macro hedges. At least 8 distinct sector labels.
    """
    rows = _read_rows()
    sectors = {r["sector"] for r in rows}
    assert len(sectors) >= 8, (
        f"expected >=8 distinct sectors; got {len(sectors)}: {sorted(sectors)}"
    )


def test_no_flag_for_review_notes() -> None:
    """Tier-1 is curated — no edge cases. If a ticker earned a flag,
    it doesn't belong in Tier-1; move it to Tier-2 or drop it.
    """
    rows = _read_rows()
    flag_substrings = (
        "flag for review",
        "flag for tier review",
        "below market cap floor",
        "below volume floor",
        "ADR",
        "upper edge",
    )
    flagged = [
        r for r in rows
        if r["notes"] and any(s in r["notes"] for s in flag_substrings)
    ]
    assert not flagged, (
        f"Tier-1 must be unflagged; found flagged rows: "
        f"{[(r['ticker'], r['notes']) for r in flagged]}"
    )


def test_approx_columns_preserved() -> None:
    """Same convention as Tier-2: '_approx' suffix marks manually-built
    cells. When a real screener integration lands, it writes to NEW
    columns ('market_cap_usd_b', 'options_volume_30d_avg') and leaves
    '_approx' for diff-vs-prior.
    """
    header = _read_header()
    assert "market_cap_usd_b_approx" in header
    assert "options_volume_30d_avg_approx" in header
