"""Phase 3.3.4.1 tests for ``historical.universe``.

Pins:
  - read_universe parses the real tier1_anchor + tier2_starter files
    without errors
  - 5-column schema enforced (ticker, sector, market_cap, vol, notes)
  - missing required columns raises
  - missing file raises FileNotFoundError
  - empty file returns ()
  - malformed rows logged + skipped (don't abort batch)
  - duplicate tickers de-duped (first wins, warning logged)
  - ticker normalisation: uppercased, validated against pattern
  - ~50 / ~250000 / blank → int / int / None
  - sector free-form (no normalisation)
  - tickers_only convenience extracts column
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uoa_detector.historical.universe import (
    UniverseEntry,
    read_universe,
    tickers_only,
)

# ---------------------------------------------------------------------------
# Real-file smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def test_tier1_anchor_csv_parses(repo_root: Path) -> None:
    """The real tier1_anchor.csv file parses cleanly."""
    path = repo_root / "data" / "universes" / "tier1_anchor.csv"
    entries = read_universe(path)
    assert len(entries) >= 15  # tier1 has ~20 anchor tickers
    tickers = tickers_only(entries)
    assert "SPY" in tickers
    assert "QQQ" in tickers


def test_tier2_starter_csv_parses(repo_root: Path) -> None:
    """The real tier2_starter.csv file parses cleanly."""
    path = repo_root / "data" / "universes" / "tier2_starter.csv"
    entries = read_universe(path)
    assert len(entries) >= 40  # tier2 has ~51
    tickers = tickers_only(entries)
    assert "PLTR" in tickers


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_missing_required_column_raises(tmp_path: Path) -> None:
    """A CSV without the required 'ticker' column raises."""
    p = tmp_path / "bad.csv"
    p.write_text("sector,market_cap_usd_b_approx\nfoo,~100\n")
    with pytest.raises(ValueError, match="missing required columns"):
        read_universe(p)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_universe(tmp_path / "does_not_exist.csv")


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("")
    assert read_universe(p) == ()


def test_header_only_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "headeronly.csv"
    p.write_text("ticker,sector,market_cap_usd_b_approx,"
                  "options_volume_30d_avg_approx,notes\n")
    assert read_universe(p) == ()


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def test_malformed_row_skipped_others_kept(tmp_path: Path) -> None:
    """A bad ticker row is dropped; valid rows continue."""
    p = tmp_path / "mix.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "AAPL,tech,~3000,~5000000,\n"
        "weird ticker with spaces!,sector,~1,~1,\n"  # ticker fails regex
        "MSFT,tech,~2500,~4000000,\n",
    )
    entries = read_universe(p)
    tickers = tickers_only(entries)
    assert tickers == ("AAPL", "MSFT")


def test_duplicate_ticker_keeps_first(tmp_path: Path) -> None:
    p = tmp_path / "dup.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "AAPL,tech,~3000,~5000000,first\n"
        "AAPL,tech,~3000,~5000000,duplicate\n"
        "MSFT,tech,~2500,~4000000,\n",
    )
    entries = read_universe(p)
    assert len(entries) == 2
    assert entries[0].notes == "first"


def test_blank_sector_row_skipped(tmp_path: Path) -> None:
    p = tmp_path / "blank.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "AAPL,,,~1,\n",  # blank sector → invalid
    )
    assert read_universe(p) == ()


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_ticker_uppercased_on_parse(tmp_path: Path) -> None:
    p = tmp_path / "lower.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "aapl,tech,~3000,,\n",
    )
    entries = read_universe(p)
    assert entries[0].ticker == "AAPL"


def test_approx_numeric_parsing() -> None:
    """~50 / ~250000 / 100 / blank / garbage → 50 / 250000 / 100 / None / None."""
    from uoa_detector.historical.universe import _parse_approx_int
    assert _parse_approx_int("~50") == 50
    assert _parse_approx_int("~250000") == 250000
    assert _parse_approx_int("100") == 100
    assert _parse_approx_int("") is None
    assert _parse_approx_int("   ") is None
    assert _parse_approx_int("garbage") is None
    assert _parse_approx_int("~ 250 ") == 250


def test_universe_entry_is_frozen() -> None:
    e = UniverseEntry(ticker="AAPL", sector="tech")
    with pytest.raises(Exception):
        e.ticker = "MSFT"  # type: ignore[misc]


def test_universe_entry_extra_forbid() -> None:
    with pytest.raises(ValueError):
        UniverseEntry(  # type: ignore[call-arg]
            ticker="AAPL", sector="tech", unknown_field=1,
        )


def test_universe_entry_invalid_ticker() -> None:
    with pytest.raises(ValueError):
        UniverseEntry(ticker="lowercase ticker", sector="tech")


def test_tickers_only_preserves_order(tmp_path: Path) -> None:
    p = tmp_path / "order.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "ZEBRA,zoo,~1,,\n"
        "ALPHA,alpha_co,~1,,\n"
        "MIDDLE,sector,~1,,\n",
    )
    entries = read_universe(p)
    assert tickers_only(entries) == ("ZEBRA", "ALPHA", "MIDDLE")
