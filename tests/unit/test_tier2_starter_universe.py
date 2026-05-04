"""Pin tests for ``data/universes/tier2_starter.csv``.

The starter universe is the input to Phase 3's combinatorial backtest
(Tier-1+single, Tier-1+fusion, Tier-2+single, Tier-2+fusion). Schema
stability matters because:

  - Backtest engine reads tickers + sector + (eventually) options volume
    to apply the universe filter.
  - 'approx' columns flag the manually-built nature of this list — every
    cell is a best-effort number from current Phase 3 prep, not a
    point-in-time-verified data feed.
  - Exclusion-list flags ('ADR — flag for review per exclusion list',
    'below market cap floor — flag', etc.) make the imperfections
    visible rather than buried. When a real screener integration lands,
    these flagged rows get filtered out automatically.

Tests verify:
  - Schema (header columns) matches the pinned set.
  - Row count is in the documented range (~50 starter tickers).
  - Tickers are unique (no accidental duplicates).
  - Sector column has reasonable diversity (no single sector dominates
    > 40% — Track B's natural tech+biotech tilt is fine, but a 90%
    tech list would defeat the universe-diversification rationale).
  - Flagged rows are visible in the notes column for downstream review.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

_UNIVERSE_PATH = Path("data/universes/tier2_starter.csv")

EXPECTED_COLUMNS = [
    "ticker",
    "sector",
    "market_cap_usd_b_approx",
    "options_volume_30d_avg_approx",
    "notes",
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
        f"Tier-2 starter universe missing at {_UNIVERSE_PATH}. "
        "Phase 3 backtest cannot run without it."
    )


def test_schema_columns_match() -> None:
    """Schema is pinned. New columns require explicit test update."""
    assert _read_header() == EXPECTED_COLUMNS


def test_row_count_in_starter_range() -> None:
    """Starter list documented as ~50 tickers; allow 40-70 for minor edits.
    A list outside this range likely indicates accidental truncation or a
    pasting error.
    """
    rows = _read_rows()
    assert 40 <= len(rows) <= 70, f"unexpected row count: {len(rows)}"


def test_tickers_are_unique() -> None:
    """Duplicate ticker entries would skew sector counts and confuse the
    backtest's per-symbol aggregation. Catch them at load time.
    """
    rows = _read_rows()
    tickers = [r["ticker"] for r in rows]
    duplicates = [t for t, c in Counter(tickers).items() if c > 1]
    assert not duplicates, f"duplicate tickers: {duplicates}"


def test_no_single_sector_dominates() -> None:
    """Sector dispersion check — Track B's natural avı tech + biotech
    ağırlıklı, OK. But a 90%+ single-sector list defeats the universe
    diversification rationale (one macro shock takes the whole portfolio
    down). Threshold: no single sector > 40%.
    """
    rows = _read_rows()
    sector_counts = Counter(r["sector"] for r in rows)
    most_common_sector, count = sector_counts.most_common(1)[0]
    fraction = count / len(rows)
    assert fraction <= 0.40, (
        f"sector concentration too high: {most_common_sector} = {fraction:.2%}"
    )


def test_flagged_rows_are_visible_in_notes() -> None:
    """Manually-built starter list has known imperfections — ADRs, sub-floor
    market caps, sub-floor volumes, upper-edge ticker. Each is flagged in
    the notes column for downstream review. This test verifies the flagging
    convention is being followed, so flagged rows aren't silently lost.
    """
    rows = _read_rows()
    flag_substrings = (
        "ADR",
        "below market cap floor",
        "below volume floor",
        "upper edge",
        "flag for tier review",
        "flag for review",
    )
    flagged = [
        r for r in rows
        if r["notes"] and any(s in r["notes"] for s in flag_substrings)
    ]
    # We expect AT LEAST a handful of flags — the starter list explicitly
    # contains edge cases for visibility. If this drops to zero, somebody
    # cleaned the notes column and we lost the audit trail.
    assert len(flagged) >= 5, (
        f"expected ≥5 flagged rows for visibility; got {len(flagged)}. "
        "Did somebody clean the notes column?"
    )


def test_market_cap_column_uses_approx_marker() -> None:
    """Column name pinned to '_approx' suffix to signal manual data origin.
    When a real screener integration lands, it should write to a NEW column
    (e.g. 'market_cap_usd_b') and leave '_approx' alone for diff-vs-prior.
    """
    header = _read_header()
    assert "market_cap_usd_b_approx" in header
    assert "options_volume_30d_avg_approx" in header


def test_no_obvious_meme_tickers_present() -> None:
    """Exclusion list per Phase 3 prep: GME, AMC, BBBY, BB, NOK and the
    classic 2021-meme cohort are excluded because their tarihsel veri is
    sample-contaminated. If somebody adds them later, this test fires.
    """
    excluded = {"GME", "AMC", "BBBY", "BB", "NOK", "AMD"}
    # AMD listed as defensive: not a meme but a frequent gamma-squeeze
    # discussion point; if we want it explicitly later, remove from set.
    rows = _read_rows()
    tickers = {r["ticker"] for r in rows}
    contaminated = tickers & excluded
    assert not contaminated, (
        f"excluded meme/legacy tickers present: {contaminated}"
    )
