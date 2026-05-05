"""Parquet schema for replay harness historical data files.

Phase 3.2.2.1: defines the on-disk format and round-trip helpers.

Schema (PyArrow):
  All 16 fields of ``RawPrint`` plus three time-tracking columns:

    - ``arrival_ts``  (timestamp[ns, UTC], nullable)
        The walltime the live system received this print. Distinct
        from the print's exchange ``timestamp``. Snapshot exporters
        (Phase 3.5) populate this from live source metadata; pre-
        existing historical dumps may leave it NULL.

    - ``replay_ts``  (timestamp[ns, UTC], nullable)
        ALWAYS NULL on disk. Filled by ``ParquetReplaySource`` at
        emission time so cross-run determinism diffs can be located
        from the data alone.

The RawPrint canonical field ``source_tags`` (tuple[str, ...]) is
stored as a Parquet list<string>. Decimals are stored as
``decimal128(20, 6)`` — 14 integer digits + 6 fractional, generous
for any options price / strike / premium.

Schema-version pin:
  The schema literal (``RAWPRINT_PARQUET_SCHEMA``) is the single
  source of truth. ``validate_schema_or_raise()`` compares an open
  parquet file's schema against the literal and raises
  ``ParquetSchemaMismatchError`` with a regenerate hint pointing at
  the snapshot exporter (when it lands in 3.5) or the test fixture
  generator (now). Any change to ``RawPrint`` that adds/removes a
  field breaks this comparison; the fix is to regenerate fixtures,
  not to silently widen the schema.

decision: schema strict-match by default rather than lenient
("ignore unknown columns") — the cost of a strict match is one
explicit "regenerate fixtures" per RawPrint schema change, and the
benefit is that production replay never silently uses stale data
where a new sub-score column is missing or a renamed field is
mapped to None.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pyarrow as pa

from uoa_detector.domain.events import FillSide, OptionType
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.errors import UOADetectorError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


# Decimal type used throughout. 20 total digits, 6 fractional is wide
# enough for any plausible options price, strike, or premium magnitude
# (max represented value: 99,999,999,999,999.999999).
_DECIMAL = pa.decimal128(20, 6)
_TS_UTC = pa.timestamp("ns", tz="UTC")


RAWPRINT_PARQUET_SCHEMA: pa.Schema = pa.schema(
    [
        # Source identity
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("source_event_id", pa.string(), nullable=False),
        # Trade identity
        pa.field("timestamp", _TS_UTC, nullable=False),
        pa.field("ticker", pa.string(), nullable=False),
        pa.field("option_type", pa.string(), nullable=False),
        pa.field("strike", _DECIMAL, nullable=False),
        pa.field("expiry", pa.date32(), nullable=False),
        pa.field("dte", pa.int32(), nullable=False),
        # Pricing
        pa.field("spot_price", _DECIMAL, nullable=False),
        pa.field("premium_paid", _DECIMAL, nullable=False),
        pa.field("option_price", _DECIMAL, nullable=False),
        pa.field("bid", _DECIMAL, nullable=False),
        pa.field("ask", _DECIMAL, nullable=False),
        pa.field("fill_side", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        # Optional fields
        pa.field("implied_volatility", pa.float64(), nullable=True),
        pa.field("open_interest", pa.int32(), nullable=True),
        pa.field("is_iso", pa.bool_(), nullable=False),
        # Source-specific tags
        pa.field("source_tags", pa.list_(pa.string()), nullable=False),
        # Replay metadata (3.2.2.1)
        pa.field("arrival_ts", _TS_UTC, nullable=True),
        pa.field("replay_ts", _TS_UTC, nullable=True),
    ],
)


class ParquetSchemaMismatchError(UOADetectorError):
    """Raised when an on-disk parquet schema doesn't match the canonical one.

    Carries the diff (missing fields, extra fields) plus a regenerate
    hint so the operator knows what to do next.
    """


class DataIntegrityError(UOADetectorError):
    """Raised when on-disk replay data violates an invariant.

    Examples: out-of-order timestamp within a file, corrupted parquet
    file, malformed row that fails RawPrint validation.
    """


def validate_schema_or_raise(actual: pa.Schema, *, file_path: Path) -> None:
    """Compare ``actual`` against ``RAWPRINT_PARQUET_SCHEMA``; raise on diff.

    Strict comparison: extra columns also fail (forces explicit
    schema-version bump). Type comparison uses ``pa.types.is_*`` rather
    than equality so timestamp/decimal precision tolerance can be
    relaxed if needed in a follow-up; today it is full equality.
    """
    expected_names = {f.name for f in RAWPRINT_PARQUET_SCHEMA}
    actual_names = {f.name for f in actual}

    missing = expected_names - actual_names
    extra = actual_names - expected_names

    if missing or extra:
        msg_lines = [
            f"Parquet schema mismatch in {file_path}.",
        ]
        if missing:
            msg_lines.append(f"  missing fields: {sorted(missing)}")
        if extra:
            msg_lines.append(f"  extra fields:   {sorted(extra)}")
        msg_lines.append(
            "Regenerate the fixture (or re-run the snapshot exporter when "
            "it lands in Phase 3.5). The on-disk schema must match the "
            "current RAWPRINT_PARQUET_SCHEMA exactly.",
        )
        raise ParquetSchemaMismatchError("\n".join(msg_lines))

    # Type-level check on shared field names.
    for name in expected_names:
        expected_field = RAWPRINT_PARQUET_SCHEMA.field(name)
        actual_field = actual.field(name)
        if expected_field.type != actual_field.type:
            msg = (
                f"Parquet schema type mismatch in {file_path}: field "
                f"{name!r} is {actual_field.type} on disk but expected "
                f"{expected_field.type}. Regenerate the fixture."
            )
            raise ParquetSchemaMismatchError(msg)


def raw_prints_to_table(prints: Iterable[RawPrint]) -> pa.Table:
    """Build a PyArrow ``Table`` from an iterable of ``RawPrint``s.

    ``arrival_ts`` and ``replay_ts`` are NULL — fixtures and snapshot
    exporters that have a real ``arrival_ts`` should populate it via
    ``write_parquet`` directly, not through this helper. ``replay_ts``
    is always written NULL on disk by contract.
    """
    rows = list(prints)

    columns: dict[str, list[object]] = {f.name: [] for f in RAWPRINT_PARQUET_SCHEMA}
    for p in rows:
        columns["source_id"].append(p.source_id)
        columns["source_event_id"].append(p.source_event_id)
        columns["timestamp"].append(p.timestamp)
        columns["ticker"].append(p.ticker)
        columns["option_type"].append(p.option_type)
        columns["strike"].append(p.strike)
        columns["expiry"].append(p.expiry)
        columns["dte"].append(p.dte)
        columns["spot_price"].append(p.spot_price)
        columns["premium_paid"].append(p.premium_paid)
        columns["option_price"].append(p.option_price)
        columns["bid"].append(p.bid)
        columns["ask"].append(p.ask)
        columns["fill_side"].append(p.fill_side)
        columns["exchange"].append(p.exchange)
        columns["implied_volatility"].append(p.implied_volatility)
        columns["open_interest"].append(p.open_interest)
        columns["is_iso"].append(p.is_iso)
        columns["source_tags"].append(list(p.source_tags))
        columns["arrival_ts"].append(None)
        columns["replay_ts"].append(None)

    return pa.Table.from_pydict(columns, schema=RAWPRINT_PARQUET_SCHEMA)


def write_parquet(
    prints: Iterable[RawPrint],
    file_path: Path,
    *,
    arrival_timestamps: list[datetime | None] | None = None,
) -> None:
    """Write an iterable of ``RawPrint``s to a parquet file.

    ``arrival_timestamps``, if provided, populates ``arrival_ts``
    column. Length must match the iterable. ``replay_ts`` is always
    written NULL — by harness contract, only the replay reader fills it.

    Compression: ``zstd`` level 3 (acceptance-doc default).
    """
    import pyarrow.parquet as pq  # local import: pyarrow.parquet pulls heavy deps

    table = raw_prints_to_table(prints)
    if arrival_timestamps is not None:
        if len(arrival_timestamps) != table.num_rows:
            msg = (
                f"arrival_timestamps length ({len(arrival_timestamps)}) "
                f"does not match RawPrint count ({table.num_rows})."
            )
            raise ValueError(msg)
        table = table.set_column(
            table.schema.get_field_index("arrival_ts"),
            "arrival_ts",
            pa.array(arrival_timestamps, type=_TS_UTC),
        )

    file_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(  # type: ignore[no-untyped-call]
        table, file_path, compression="zstd", compression_level=3,
    )


def row_to_raw_print(row: dict[str, object]) -> RawPrint:
    """Reconstruct a ``RawPrint`` from a parquet row dict.

    The reverse of ``raw_prints_to_table``. Used by the replay reader.
    Validation runs through Pydantic, so a malformed row (out of range
    DTE, wrong option_type literal, etc.) raises ``ValidationError``;
    callers should wrap and re-raise as ``DataIntegrityError`` for
    operator clarity.

    The replay metadata (``arrival_ts``, ``replay_ts``, source_tags
    list-vs-tuple) is normalised on the way in — Pydantic accepts
    ``list[str]`` for ``tuple[str, ...]`` fields.
    """
    iv_raw = row["implied_volatility"]
    oi_raw = row["open_interest"]
    return RawPrint(
        source_id=str(row["source_id"]),
        source_event_id=str(row["source_event_id"]),
        timestamp=_ensure_utc(row["timestamp"]),
        ticker=str(row["ticker"]),
        option_type=cast("OptionType", row["option_type"]),
        strike=Decimal(str(row["strike"])),
        expiry=cast("date", row["expiry"]),
        dte=cast("int", row["dte"]),
        spot_price=Decimal(str(row["spot_price"])),
        premium_paid=Decimal(str(row["premium_paid"])),
        option_price=Decimal(str(row["option_price"])),
        bid=Decimal(str(row["bid"])),
        ask=Decimal(str(row["ask"])),
        fill_side=cast("FillSide", row["fill_side"]),
        exchange=str(row["exchange"]),
        implied_volatility=cast("float | None", iv_raw),
        open_interest=cast("int | None", oi_raw),
        is_iso=bool(row["is_iso"]),
        source_tags=tuple(cast("list[str]", row.get("source_tags") or [])),
    )


def _ensure_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        msg = f"timestamp value is not a datetime: {value!r}"
        raise DataIntegrityError(msg)
    if value.tzinfo is None:
        # PyArrow always returns tz-aware when schema is timestamp[tz=UTC],
        # but defensive: fold to UTC if a naive datetime sneaks in.
        return value.replace(tzinfo=UTC)
    return value


# Synthetic fixture generator — produces 10 hand-crafted rows used by
# tests. Kept here (as opposed to in tests/) so the same code seeds
# the fixture file on demand and can be reused by the replay-harness
# integration test in 3.2.2.3.

def make_synthetic_aapl_2025_06_fixture() -> list[RawPrint]:
    """10 hand-crafted RawPrints for AAPL 2025-06.

    Spread across 5 trading days with strict-monotonic timestamps,
    mixed option types, mixed strikes, all marked as the same
    synthetic source. Used by the replay-harness fixture file and
    cross-checked by the round-trip tests.
    """
    base_ts = datetime(2025, 6, 9, 14, 35, tzinfo=UTC)  # Monday open
    rows: list[RawPrint] = []
    for i in range(10):
        # 1 print per row, spaced ~6 hours apart so they span the
        # week; this is intentionally artificial.
        ts = datetime(
            2025, 6, 9 + (i // 2), 14 + (i % 2) * 6, 35, tzinfo=UTC,
        )
        is_call = i % 2 == 0
        strike = Decimal("200") + Decimal(i)
        rows.append(
            RawPrint(
                source_id="synthetic_replay",
                source_event_id=f"fixture-aapl-{i:03d}",
                timestamp=ts,
                ticker="AAPL",
                option_type="call" if is_call else "put",
                strike=strike,
                expiry=date(2025, 7, 18),
                dte=39 - (i // 2),
                spot_price=Decimal("198.50"),
                premium_paid=Decimal(1000 + i * 100),
                option_price=Decimal("1.50") + Decimal(i) / Decimal(10),
                bid=Decimal("1.45") + Decimal(i) / Decimal(10),
                ask=Decimal("1.55") + Decimal(i) / Decimal(10),
                fill_side="above_ask" if is_call else "below_bid",
                exchange="CBOE",
                implied_volatility=0.40 + i * 0.005,
                open_interest=1500 + i * 100,
                is_iso=(i % 3 == 0),
                source_tags=(),
            ),
        )
    # Sanity: monotonic ascending timestamps.
    assert all(rows[i].timestamp <= rows[i + 1].timestamp for i in range(9))
    # Reference base_ts so it isn't unused.
    _ = base_ts
    return rows
