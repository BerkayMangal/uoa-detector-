"""Phase 3.6.4 — self-derived IV (M24) + OI (M27) provider tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uoa_detector.providers.iv_history import IVHistoryProvider
from uoa_detector.providers.open_interest import OpenInterestProvider
from uoa_detector.sources.thetadata_derived.chain_history import ChainHistory
from uoa_detector.sources.thetadata_derived.iv_oi import (
    ThetaDataIVHistoryProvider,
    ThetaDataOpenInterestProvider,
)

_EXPIRY = date(2025, 9, 19)


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    pq.write_table(
        pa.table({
            "snapshot_date": pa.array([r["snapshot_date"] for r in rows], pa.date32()),
            "strike": pa.array([r["strike"] for r in rows], pa.float64()),
            "expiry": pa.array([r["expiry"] for r in rows], pa.date32()),
            "option_type": pa.array([r["option_type"] for r in rows], pa.string()),
            "open_interest": pa.array([r["open_interest"] for r in rows], pa.int64()),
            "implied_volatility": pa.array([r["implied_volatility"] for r in rows], pa.float64()),
            "spot": pa.array([r["spot"] for r in rows], pa.float64()),
        }),
        path,
    )


def _make_history(tmp_path: Path, *, days: int = 25) -> ChainHistory:
    base = date(2025, 7, 1)
    rows: list[dict[str, object]] = []
    for i in range(days):
        d = base + timedelta(days=i)
        atm_iv = 0.20 + 0.20 * (i / (days - 1))  # 0.20 → 0.40 linearly
        # ATM contract (strike 100 == spot) and a far contract (strike 150).
        rows.append({
            "snapshot_date": d, "strike": 100.0, "expiry": _EXPIRY,
            "option_type": "call", "open_interest": 1000 + i * 10,
            "implied_volatility": atm_iv, "spot": 100.0,
        })
        rows.append({
            "snapshot_date": d, "strike": 150.0, "expiry": _EXPIRY,
            "option_type": "call", "open_interest": 500,
            "implied_volatility": 0.9, "spot": 100.0,
        })
    _write(tmp_path / "TSLA.parquet", rows)
    return ChainHistory(tmp_path)


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ChainHistory
# ---------------------------------------------------------------------------


def test_oi_at_most_recent_on_or_before(tmp_path: Path) -> None:
    h = _make_history(tmp_path)
    # day index 5 → OI = 1000 + 50 = 1050
    res = h.oi_at("TSLA", Decimal("100.0"), _EXPIRY, "call", _at(date(2025, 7, 6)))
    assert res == (date(2025, 7, 6), 1050)
    # before the first snapshot → None
    assert h.oi_at("TSLA", Decimal("100.0"), _EXPIRY, "call", _at(date(2025, 6, 1))) is None


def test_oi_next_day_strictly_after(tmp_path: Path) -> None:
    h = _make_history(tmp_path)
    res = h.oi_next_day("TSLA", Decimal("100.0"), _EXPIRY, "call", date(2025, 7, 6))
    assert res == (date(2025, 7, 7), 1060)  # day index 6
    # after the last day → None
    assert h.oi_next_day("TSLA", Decimal("100.0"), _EXPIRY, "call", date(2025, 8, 1)) is None


def test_atm_iv_rank_uses_nearest_strike_and_ranks(tmp_path: Path) -> None:
    h = _make_history(tmp_path, days=25)
    # Last day: ATM (strike 100) IV = 0.40 = the max → rank ~100.
    rank_last = h.atm_iv_rank("TSLA", _at(date(2025, 7, 25)))
    assert rank_last is not None
    assert rank_last == pytest.approx(100.0, abs=1e-6)
    # First day: IV = min → rank ~0. But < _MIN_RANK_DAYS history → None.
    assert h.atm_iv_rank("TSLA", _at(date(2025, 7, 1))) is None


def test_atm_iv_rank_none_before_min_history(tmp_path: Path) -> None:
    h = _make_history(tmp_path, days=10)  # < 20 days
    assert h.atm_iv_rank("TSLA", _at(date(2025, 7, 10))) is None


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oi_provider_at_and_next_day(tmp_path: Path) -> None:
    p = ThetaDataOpenInterestProvider(_make_history(tmp_path))
    snap = await p.at("TSLA", Decimal("100.0"), _EXPIRY, "call", _at(date(2025, 7, 6)))
    assert snap is not None
    assert snap.open_interest == 1050
    nxt = await p.next_day("TSLA", Decimal("100.0"), _EXPIRY, "call", date(2025, 7, 6))
    assert nxt is not None
    assert nxt.open_interest == 1060
    # unknown contract → None
    assert await p.at("TSLA", Decimal("999"), _EXPIRY, "call", _at(date(2025, 7, 6))) is None


@pytest.mark.asyncio
async def test_iv_provider_reports_iv_and_rank(tmp_path: Path) -> None:
    p = ThetaDataIVHistoryProvider(_make_history(tmp_path))
    snap = await p.iv_rank_at("TSLA", Decimal("100.0"), _EXPIRY, "call", _at(date(2025, 7, 25)))
    assert snap is not None
    assert snap.implied_volatility == pytest.approx(0.40, abs=1e-6)
    assert snap.iv_rank_252d == pytest.approx(100.0, abs=1e-6)
    # unknown contract → None
    assert await p.iv_rank_at("TSLA", Decimal("999"), _EXPIRY, "call", _at(date(2025, 7, 25))) is None


def test_providers_satisfy_protocols(tmp_path: Path) -> None:
    h = _make_history(tmp_path)
    assert isinstance(ThetaDataOpenInterestProvider(h), OpenInterestProvider)
    assert isinstance(ThetaDataIVHistoryProvider(h), IVHistoryProvider)
