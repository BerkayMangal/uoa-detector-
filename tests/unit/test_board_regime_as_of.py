"""Phase 5.2.B-fix5: the regime band ages its daily readings by the vendor's own date.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B5 ("Staleness. Any
source older than ``regime.max_source_age_seconds`` renders bilinmiyor").

Review finding FB-H5: the SPY IV term-structure and VIX readings were aged by
our fetch time only. Their ``as_of`` was parsed, stored and then ignored, so a
week-old vendor reading rendered as "2 dk önce alındı" and drove the curve
tripwire under "fikrimi ne değiştirir". This is the defect class decision P3
shipped a hotfix for (a 6-day-old gamma as_of shown as current), on a new
surface.

Pins:
  - a curve or VIX reading whose vendor date is older than the board's ET date
    reads ``bilinmiyor`` and its chip is not known, however fresh our fetch was;
  - the curve tripwire is unknown when the curve is, and never fires off a stale
    reading;
  - a reading dated today is fresh, and its chip discloses that date;
  - a reading without a vendor date keeps the fetch-time rule;
  - the new copy passes ``ensure_clean``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import regime as rg
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings

from tests.unit.test_board_regime import _FETCH, _FakeClient, _paths

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

_REPO = Path(__file__).resolve().parents[2]
_NOW = _FETCH + timedelta(seconds=30)
_STALE_DAY = "2026-09-08"  # a week before the fetch, which is dated 2026-09-15
_TODAY = "2026-09-15"


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'regime.db'}")
    rg.ensure_regime_tables(engine)
    return session_factory(engine)


def _term(day: str, dte: int, vol: str, expiry: str, ticker: str = "SPY",
          move: str = "1", perc: str = "0.01") -> dict[str, Any]:
    return {"date": day, "ticker": ticker, "volatility": vol, "expiry": expiry,
            "dte": dte, "implied_move": move, "implied_move_perc": perc}


def _spy_curve(day: str) -> dict[str, Any]:
    return {"data": [
        _term(day, 31, "0.142251103140261", "2026-10-16"),
        _term(day, 94, "0.1567835746493346", "2026-12-18"),
    ]}


def _vix_curve(day: str) -> dict[str, Any]:
    return {"data": [
        _term(day, 36, "0.71", "2026-10-21", "VIX", "2.855231326748495", "0.1631560758141997"),
        _term(day, 64, "0.70", "2026-11-18", "VIX", "3.77843197749135", "0.2159103987137914"),
    ]}


def _undated_curve() -> dict[str, Any]:
    rows = _spy_curve(_TODAY)["data"]
    for row in rows:
        row.pop("date")
    return {"data": rows}


async def _band(
    sessions: sessionmaker[Session], settings: BoardSettings, payloads: dict[str, Any],
) -> rg.RegimeBand:
    client = _FakeClient({**_paths(), **payloads})
    await rg.refresh_regime(client, sessions, settings=settings, now=_FETCH)  # type: ignore[arg-type]
    with sessions() as session:
        inputs = rg.load_regime_inputs(session, now=_NOW)
    return rg.build_regime_band(inputs, settings=settings, now=_NOW)


def _chip(band: rg.RegimeBand, key: str) -> rg.RegimeChip:
    return next(chip for chip in band.chips if chip.key == key)


async def test_a_stale_vendor_date_reads_unknown_however_fresh_the_fetch(
    sessions: sessionmaker[Session], settings: BoardSettings,
) -> None:
    band = await _band(sessions, settings, {
        rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): _spy_curve(_STALE_DAY),
        rg.TERM_STRUCTURE_PATH.format(ticker="VIX"): _vix_curve(_STALE_DAY),
    })
    curve = _chip(band, "curve")
    assert curve.known is False
    assert curve.text == "SPY IV vadesi: bilinmiyor"
    vix = _chip(band, "vix_spot")
    assert vix.known is False
    assert vix.text == "VIX: bilinmiyor"
    assert "SPY IV vadesi: bilinmiyor" in band.sentence_parts
    assert "VIX: bilinmiyor" in band.sentence_parts


async def test_the_curve_tripwire_is_unknown_when_the_curve_is(
    sessions: sessionmaker[Session], settings: BoardSettings,
) -> None:
    inverted = {"data": [
        _term(_STALE_DAY, 30, "0.20", "2026-10-15"),
        _term(_STALE_DAY, 90, "0.15", "2026-12-14"),
    ]}
    band = await _band(sessions, settings, {
        rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): inverted,
    })
    curve_wire = band.tripwires[2]
    assert curve_wire.key == "curve"
    assert curve_wire.fired is None  # a week-old reading never fires the tripwire
    assert curve_wire.status_text == "bilinmiyor"


async def test_a_reading_dated_today_is_fresh_and_discloses_its_date(
    sessions: sessionmaker[Session], settings: BoardSettings,
) -> None:
    band = await _band(sessions, settings, {
        rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): _spy_curve(_TODAY),
        rg.TERM_STRUCTURE_PATH.format(ticker="VIX"): _vix_curve(_TODAY),
    })
    curve = _chip(band, "curve")
    assert curve.known is True
    assert curve.text.startswith("SPY IV vadesi contango (IV31G %14.2 · IV94G %15.7)")
    assert curve.text.endswith("(15.09 itibarıyla)")
    vix = _chip(band, "vix_spot")
    assert vix.known is True
    assert vix.text.startswith("VIX ≈ ")
    assert vix.text.endswith("(15.09 itibarıyla)")
    # The sentence keeps the contract's wording: the date is disclosed on the chip.
    assert "SPY IV vadesi contango (IV31G %14.2 · IV94G %15.7)" in band.sentence_parts


async def test_a_reading_without_a_vendor_date_keeps_the_fetch_time_rule(
    sessions: sessionmaker[Session], settings: BoardSettings,
) -> None:
    band = await _band(sessions, settings, {
        rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): _undated_curve(),
    })
    curve = _chip(band, "curve")
    assert curve.known is True  # 30 s old by our fetch clock, and no date to check
    assert "itibarıyla" not in curve.text


def test_the_as_of_disclosure_is_frozen_and_clean() -> None:
    assert rg.AS_OF_TEMPLATE == "({as_of} itibarıyla)"
    assert ensure_clean(rg.AS_OF_TEMPLATE) == rg.AS_OF_TEMPLATE
