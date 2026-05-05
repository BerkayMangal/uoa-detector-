"""Phase 3.2.2.1 tests for the replay-harness parquet schema layer.

Covers:
  - canonical schema constants are stable (column count, names, types)
  - RawPrint round-trip preserves all fields
  - schema-mismatch detection raises with regenerate hint
  - the synthetic fixture file matches the canonical schema
  - the synthetic fixture is timestamp-monotonic
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest.parquet_schema import (
    RAWPRINT_PARQUET_SCHEMA,
    ParquetSchemaMismatchError,
    make_synthetic_aapl_2025_06_fixture,
    raw_prints_to_table,
    row_to_raw_print,
    validate_schema_or_raise,
    write_parquet,
)
from uoa_detector.domain.raw_print import RawPrint

# ---------------------------------------------------------------------------
# Schema constants — pin tests
# ---------------------------------------------------------------------------


def test_schema_has_expected_column_count() -> None:
    """16 RawPrint fields + 3 replay-metadata fields + source_tags + dte = 21."""
    assert len(RAWPRINT_PARQUET_SCHEMA) == 21


def test_schema_column_names_match_pin() -> None:
    """If you add or remove a RawPrint field, this test must fail.

    The cost of false alarms here is one explicit fixture regeneration;
    the cost of NOT having this test is silent schema drift.
    """
    expected = {
        "source_id", "source_event_id",
        "timestamp", "ticker", "option_type", "strike", "expiry", "dte",
        "spot_price", "premium_paid", "option_price", "bid", "ask",
        "fill_side", "exchange",
        "implied_volatility", "open_interest", "is_iso", "source_tags",
        "arrival_ts", "replay_ts",
    }
    actual = {f.name for f in RAWPRINT_PARQUET_SCHEMA}
    assert actual == expected


def test_replay_metadata_fields_are_nullable() -> None:
    """arrival_ts and replay_ts are populated by the harness, not on disk."""
    assert RAWPRINT_PARQUET_SCHEMA.field("arrival_ts").nullable is True
    assert RAWPRINT_PARQUET_SCHEMA.field("replay_ts").nullable is True


def test_required_fields_are_not_nullable() -> None:
    """RawPrint required fields stay non-nullable on disk."""
    for name in (
        "source_id", "source_event_id",
        "timestamp", "ticker", "option_type", "strike", "expiry", "dte",
        "spot_price", "premium_paid", "option_price", "bid", "ask",
        "fill_side", "exchange", "is_iso", "source_tags",
    ):
        assert RAWPRINT_PARQUET_SCHEMA.field(name).nullable is False, name


# ---------------------------------------------------------------------------
# Round-trip — RawPrint → parquet table → RawPrint
# ---------------------------------------------------------------------------


def _assert_round_trip(prints: list[RawPrint]) -> None:
    table = raw_prints_to_table(prints)
    assert table.schema == RAWPRINT_PARQUET_SCHEMA
    rows = table.to_pylist()
    assert len(rows) == len(prints)
    for original, row in zip(prints, rows, strict=True):
        recovered = row_to_raw_print(row)
        # Compare all fields except source_tags (list-vs-tuple normalisation
        # already applied) — Pydantic equality covers everything else.
        assert recovered == original, f"mismatch at {original.source_event_id}"


def test_round_trip_synthetic_fixture() -> None:
    """The 10-row synthetic fixture round-trips field-perfect."""
    prints = make_synthetic_aapl_2025_06_fixture()
    _assert_round_trip(prints)


def test_round_trip_with_optional_nones() -> None:
    """RawPrints whose IV / OI are None round-trip cleanly."""
    from datetime import UTC, date, datetime
    from decimal import Decimal

    p = RawPrint(
        source_id="t",
        source_event_id="e1",
        timestamp=datetime(2025, 6, 1, 14, 30, tzinfo=UTC),
        ticker="MSFT",
        option_type="put",
        strike=Decimal("400"),
        expiry=date(2025, 7, 18),
        dte=47,
        spot_price=Decimal("420"),
        premium_paid=Decimal("5000"),
        option_price=Decimal("8.50"),
        bid=Decimal("8.40"),
        ask=Decimal("8.60"),
        fill_side="midpoint",
        exchange="ARCA",
        # implied_volatility / open_interest left as None
        is_iso=False,
        source_tags=("sweep", "newsworthy"),
    )
    _assert_round_trip([p])


def test_round_trip_preserves_source_tags() -> None:
    """source_tags is a tuple in Python, list in Parquet — roundtrip back."""
    from datetime import UTC, date, datetime
    from decimal import Decimal

    p = RawPrint(
        source_id="t",
        source_event_id="tag",
        timestamp=datetime(2025, 6, 1, 14, 30, tzinfo=UTC),
        ticker="NVDA",
        option_type="call",
        strike=Decimal("1000"),
        expiry=date(2025, 7, 18),
        dte=47,
        spot_price=Decimal("995"),
        premium_paid=Decimal("10000"),
        option_price=Decimal("12.00"),
        bid=Decimal("11.95"),
        ask=Decimal("12.05"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        source_tags=("sweep", "smart_money", "iv_surge"),
    )
    table = raw_prints_to_table([p])
    rows = table.to_pylist()
    recovered = row_to_raw_print(rows[0])
    assert recovered.source_tags == ("sweep", "smart_money", "iv_surge")
    assert isinstance(recovered.source_tags, tuple)


# ---------------------------------------------------------------------------
# Fixture file on disk matches canonical schema
# ---------------------------------------------------------------------------


_FIXTURE_PATH = Path("tests/fixtures/historical/synthetic/AAPL/2025-06.parquet")


def test_fixture_file_exists() -> None:
    assert _FIXTURE_PATH.exists(), (
        f"Replay fixture missing at {_FIXTURE_PATH}. Regenerate with:\n"
        "  uv run python -c 'from uoa_detector.backtest.parquet_schema "
        "import make_synthetic_aapl_2025_06_fixture, write_parquet; "
        "from pathlib import Path; "
        "write_parquet(make_synthetic_aapl_2025_06_fixture(), "
        'Path("tests/fixtures/historical/synthetic/AAPL/2025-06.parquet"))\''
    )


def test_fixture_schema_matches_canonical() -> None:
    """The fixture file's parquet schema is exactly the canonical schema.

    Strict equality. If RawPrint gains a field, this fails until the
    fixture is regenerated — which is the right kind of friction.
    """
    table = pq.read_table(_FIXTURE_PATH)
    assert table.schema == RAWPRINT_PARQUET_SCHEMA


def test_fixture_has_ten_rows() -> None:
    assert pq.read_table(_FIXTURE_PATH).num_rows == 10


def test_fixture_is_timestamp_monotonic() -> None:
    """Streaming validation in 3.2.2.2 will rely on this; confirm the
    fixture itself respects the contract."""
    table = pq.read_table(_FIXTURE_PATH)
    timestamps = table.column("timestamp").to_pylist()
    assert all(
        timestamps[i] <= timestamps[i + 1] for i in range(len(timestamps) - 1)
    ), "fixture timestamps are not ascending"


def test_fixture_replay_ts_is_all_null_on_disk() -> None:
    """Contract: replay_ts is harness-written; on-disk it is always NULL.

    Phase 3.5+ snapshot exporter must respect this — if a future
    exporter sets replay_ts on disk, this test fails and we catch it.
    """
    table = pq.read_table(_FIXTURE_PATH)
    replay_ts = table.column("replay_ts").to_pylist()
    assert all(v is None for v in replay_ts)


# ---------------------------------------------------------------------------
# Schema-mismatch detection
# ---------------------------------------------------------------------------


def test_validate_schema_accepts_canonical(tmp_path: Path) -> None:
    """A file with the canonical schema validates silently."""
    p = tmp_path / "good.parquet"
    write_parquet(make_synthetic_aapl_2025_06_fixture(), p)
    table = pq.read_table(p)
    validate_schema_or_raise(table.schema, file_path=p)  # no raise


def test_validate_schema_raises_on_missing_field(tmp_path: Path) -> None:
    """An older parquet missing a field raises with a regenerate hint."""
    bad = pa.schema(
        [f for f in RAWPRINT_PARQUET_SCHEMA if f.name != "implied_volatility"],
    )
    fake_path = tmp_path / "missing.parquet"
    with pytest.raises(ParquetSchemaMismatchError) as exc:
        validate_schema_or_raise(bad, file_path=fake_path)
    msg = str(exc.value)
    assert "missing fields" in msg
    assert "implied_volatility" in msg
    assert "Regenerate" in msg


def test_validate_schema_raises_on_extra_field(tmp_path: Path) -> None:
    """Unknown columns also fail — strict match, not lenient."""
    bad = pa.schema(
        [*RAWPRINT_PARQUET_SCHEMA, pa.field("future_ml_score", pa.float64())],
    )
    fake_path = tmp_path / "extra.parquet"
    with pytest.raises(ParquetSchemaMismatchError) as exc:
        validate_schema_or_raise(bad, file_path=fake_path)
    msg = str(exc.value)
    assert "extra fields" in msg
    assert "future_ml_score" in msg


def test_validate_schema_raises_on_type_mismatch(tmp_path: Path) -> None:
    """If a field's type drifts (e.g. dte int32 → int64), raise.

    This catches snapshot exporters that change column types under us.
    """
    fields = []
    for f in RAWPRINT_PARQUET_SCHEMA:
        if f.name == "dte":
            fields.append(pa.field("dte", pa.int64(), nullable=False))
        else:
            fields.append(f)
    bad = pa.schema(fields)
    fake_path = tmp_path / "wrong_type.parquet"
    with pytest.raises(ParquetSchemaMismatchError) as exc:
        validate_schema_or_raise(bad, file_path=fake_path)
    assert "type mismatch" in str(exc.value)
    assert "dte" in str(exc.value)


def test_arrival_ts_can_be_populated(tmp_path: Path) -> None:
    """write_parquet supports populating arrival_ts (snapshot exporter path)."""
    from datetime import UTC, datetime

    prints = make_synthetic_aapl_2025_06_fixture()
    arrival = [datetime(2025, 6, 9, 14, 35, i, tzinfo=UTC) for i in range(10)]
    p = tmp_path / "with_arrival.parquet"
    write_parquet(prints, p, arrival_timestamps=arrival)

    table = pq.read_table(p)
    arrival_back = table.column("arrival_ts").to_pylist()
    assert all(v is not None for v in arrival_back)
    # replay_ts still NULL — by contract.
    assert all(v is None for v in table.column("replay_ts").to_pylist())
