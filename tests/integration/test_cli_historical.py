"""Phase 3.2.2.4 — CLI integration tests for ``--source historical``.

Smoke-level: drive the typer CLI with the synthetic fixture as the
historical data, write to a SQLite store, assert the database
contents reflect the fixture (10 signals, 1 finished run, etc.).
Lower-level harness behaviour is covered by tests/unit/test_parquet_replay.py
and tests/unit/test_replay_fusion.py — this module is the
end-to-end wiring confirmation.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from uoa_detector.backtest.parquet_schema import (
    write_parquet,
)
from uoa_detector.cli import app
from uoa_detector.domain.raw_print import RawPrint

runner = CliRunner()


def _build_print(
    ts: datetime,
    *,
    ticker: str = "AAPL",
    event_id: str = "e0",
    source_id: str = "synthetic_replay",
) -> RawPrint:
    return RawPrint(
        source_id=source_id,
        source_event_id=event_id,
        timestamp=ts,
        ticker=ticker,
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198.50"),
        premium_paid=Decimal("1000"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        implied_volatility=0.45,
        open_interest=1500,
        is_iso=False,
        source_tags=(),
    )


# ---------------------------------------------------------------------------
# Validation: --source historical requires --data-dir
# ---------------------------------------------------------------------------


def test_historical_source_requires_data_dir() -> None:
    result = runner.invoke(app, ["run", "--source", "historical"])
    assert result.exit_code != 0
    assert "--data-dir is required" in result.output


def test_unknown_source_rejected() -> None:
    result = runner.invoke(app, ["run", "--source", "polygon"])
    assert result.exit_code != 0
    # Phase 3.3.5 added 'live' to the choice list. Typer wraps the
    # error inside a box that line-breaks; check the substrings
    # individually to be wrapping-tolerant.
    assert "synthetic" in result.output
    assert "historical" in result.output
    assert "live" in result.output
    assert "polygon" in result.output


# ---------------------------------------------------------------------------
# Smoke: drive synthetic fixture through CLI, write SQLite store
# ---------------------------------------------------------------------------


def test_historical_source_drives_synthetic_fixture(tmp_path: Path) -> None:
    """``--source historical --data-dir <fixtures>`` writes 10 signals."""
    db_path = tmp_path / "smoke.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--source", "historical",
            "--data-dir", "tests/fixtures/historical/synthetic",
            "--store", f"sqlite:{db_path}",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, result.output
    # The default-fixture has 10 rows; pipeline emits one signal per
    # canonicalised event.
    n_lines = sum(1 for line in result.stdout.splitlines() if line.strip())
    assert n_lines == 10

    # SQLite store reflects the run + signals.
    conn = sqlite3.connect(db_path)
    try:
        table_names = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        )
        assert table_names == [
            "alembic_version", "backtest_run", "backtest_run_error", "signal",
        ]
        assert conn.execute("SELECT count(*) FROM backtest_run").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM backtest_run WHERE finished_at IS NOT NULL",
            ).fetchone()[0] == 1
        )
        assert conn.execute("SELECT count(*) FROM signal").fetchone()[0] == 10
        tickers = sorted(
            {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM signal")}
        )
        assert tickers == ["AAPL"]
    finally:
        conn.close()


def test_historical_source_with_ticker_filter(tmp_path: Path) -> None:
    """``--tickers AAPL,MSFT`` restricts the replay scope."""
    # Build a per-ticker fixture under tmp.
    for ticker in ("AAPL", "MSFT", "NVDA"):
        d = tmp_path / "data" / ticker
        d.mkdir(parents=True)
        write_parquet(
            [
                _build_print(
                    datetime(2025, 6, 9, 14, 30, tzinfo=UTC),
                    ticker=ticker, event_id=f"{ticker}-0",
                ),
            ],
            d / "2025-06.parquet",
        )

    db_path = tmp_path / "filter.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--source", "historical",
            "--data-dir", str(tmp_path / "data"),
            "--tickers", "AAPL,MSFT",
            "--store", f"sqlite:{db_path}",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(db_path)
    try:
        # Only AAPL + MSFT rows persisted.
        tickers = sorted(
            {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM signal")}
        )
        assert tickers == ["AAPL", "MSFT"]
    finally:
        conn.close()


def test_historical_source_with_date_range_filter(tmp_path: Path) -> None:
    """``--from 2025-02 --to 2025-03`` restricts months."""
    aapl_dir = tmp_path / "data" / "AAPL"
    aapl_dir.mkdir(parents=True)
    for month in (1, 2, 3, 4):
        write_parquet(
            [
                _build_print(
                    datetime(2025, month, 9, 14, 30, tzinfo=UTC),
                    event_id=f"month-{month}",
                ),
            ],
            aapl_dir / f"2025-{month:02d}.parquet",
        )

    db_path = tmp_path / "range.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--source", "historical",
            "--data-dir", str(tmp_path / "data"),
            "--from", "2025-02",
            "--to", "2025-03",
            "--store", f"sqlite:{db_path}",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(db_path)
    try:
        # Two months × 1 row → 2 signals.
        assert conn.execute("SELECT count(*) FROM signal").fetchone()[0] == 2
    finally:
        conn.close()
