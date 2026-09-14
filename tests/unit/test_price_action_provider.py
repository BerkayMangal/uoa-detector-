"""Phase 3.6.5 — self-derived price-action provider (M23) tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uoa_detector.providers.price_action import PriceActionProvider
from uoa_detector.sources.thetadata_derived.price_action import (
    SpotSeries,
    ThetaDataPriceActionProvider,
)


def _write_bars(path: Path, bars: list[tuple[datetime, float]]) -> None:
    pq.write_table(
        pa.table({
            "minute": pa.array([b[0] for b in bars], pa.timestamp("us", tz="UTC")),
            "spot": pa.array([b[1] for b in bars], pa.float64()),
        }),
        path,
    )


def _series(tmp_path: Path, bars: list[tuple[datetime, float]]) -> SpotSeries:
    _write_bars(tmp_path / "AMD.parquet", bars)
    return SpotSeries(tmp_path)


_D1 = datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


def test_spot_at_most_recent_and_staleness(tmp_path: Path) -> None:
    s = _series(tmp_path, [(_D1, 100.0), (_D1 + timedelta(minutes=10), 102.0)])
    assert s.spot_at("AMD", _D1 + timedelta(minutes=10)) == (_D1 + timedelta(minutes=10), 102.0)
    # 5 min after last bar → fresh
    assert s.spot_at("AMD", _D1 + timedelta(minutes=15)) == (_D1 + timedelta(minutes=10), 102.0)
    # 31 min after last bar → stale → None
    assert s.spot_at("AMD", _D1 + timedelta(minutes=41)) is None
    # before first bar → None
    assert s.spot_at("AMD", _D1 - timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_intraday_move_pct(tmp_path: Path) -> None:
    p = ThetaDataPriceActionProvider(
        _series(tmp_path, [(_D1, 100.0), (_D1 + timedelta(minutes=10), 102.0)]),
    )
    mv = await p.get_intraday_price_movement("AMD", _D1 + timedelta(minutes=10), 10)
    assert mv is not None
    assert mv.move_pct == pytest.approx(2.0, abs=1e-9)
    assert mv.spot_at == Decimal("102.0")
    assert mv.spot_lookback_ago == Decimal("100.0")


@pytest.mark.asyncio
async def test_overnight_gap_returns_none(tmp_path: Path) -> None:
    d2 = datetime(2025, 7, 2, 14, 0, tzinfo=UTC)
    p = ThetaDataPriceActionProvider(
        _series(tmp_path, [(_D1, 100.0), (d2, 110.0)]),
    )
    # lookback reaches into the prior day → overnight → None
    mv = await p.get_intraday_price_movement("AMD", d2, 60 * 24)
    assert mv is None


@pytest.mark.asyncio
async def test_missing_data_returns_none(tmp_path: Path) -> None:
    p = ThetaDataPriceActionProvider(_series(tmp_path, [(_D1, 100.0)]))
    assert await p.get_intraday_price_movement("NOPE", _D1, 10) is None
    snap = await p.snapshot_at("AMD", _D1)
    assert snap is not None
    assert snap.spot == Decimal("100.0")


def test_provider_satisfies_protocol(tmp_path: Path) -> None:
    p = ThetaDataPriceActionProvider(_series(tmp_path, [(_D1, 100.0)]))
    assert isinstance(p, PriceActionProvider)
