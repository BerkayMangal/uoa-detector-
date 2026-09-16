"""Phase 5.2.B5a: regime band data layer and pure band (webapp/board/regime.py).

Fixtures are trimmed from the 2026-09-15 regime probes (market tide 5m, SPY/QQQ
spot exposures, gex levels source=oi, SPY and VIX term structure, SPY greek exposure).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, inspect, select
from webapp.board import regime as rg
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_FETCH = datetime(2026, 9, 15, 15, 28, 30, tzinfo=UTC)  # 11:28:30 ET

_TIDE: dict[str, Any] = {"date": "2026-09-15", "data": [
    {"timestamp": "2026-09-15T11:05:00-04:00", "date": "2026-09-15",
     "net_call_premium": "-109675998.0000", "net_put_premium": "24338036.0000", "net_volume": -624633},
    {"timestamp": "2026-09-15T11:10:00-04:00", "date": "2026-09-15",
     "net_call_premium": "-115202789.0000", "net_put_premium": "28060288.0000", "net_volume": -636636},
    {"timestamp": "2026-09-15T11:15:00-04:00", "date": "2026-09-15",
     "net_call_premium": "-112199521.0000", "net_put_premium": "22661935.0000", "net_volume": -584814},
    {"timestamp": "2026-09-15T11:20:00-04:00", "date": "2026-09-15",
     "net_call_premium": "-109857642.0000", "net_put_premium": "15568937.0000", "net_volume": -543763},
    {"timestamp": "2026-09-15T11:25:00-04:00", "date": "2026-09-15",
     "net_call_premium": "-108057414.0000", "net_put_premium": "13803368.0000", "net_volume": -533015},
]}

_SPOT_SPY: dict[str, Any] = {"data": [
    {"time": "2026-09-15T10:30:33.485000Z", "price": "758.45",
     "gamma_per_one_percent_move_oi": "-26855278769.52"},  # pre-market, dropped
    {"time": "2026-09-15T13:32:58.000000Z", "price": "759.17",
     "gamma_per_one_percent_move_oi": "-15846195965.93"},
    {"time": "2026-09-15T15:27:58.000000Z", "price": "757.45",
     "gamma_per_one_percent_move_oi": "-18899610901.53"},
    {"time": "2026-09-15T15:28:10.000000Z", "price": "757.38",
     "gamma_per_one_percent_move_oi": "-18896117834.97"},
]}

_SPOT_QQQ: dict[str, Any] = {"data": [
    {"time": "2026-09-15T10:43:25.850000Z", "price": "707.37",
     "gamma_per_one_percent_move_oi": "-8168638597.09"},
    {"time": "2026-09-15T15:27:59.193000Z", "price": "705.7828",
     "gamma_per_one_percent_move_oi": "-9626682789.3"},
    {"time": "2026-09-15T15:28:10.000000Z", "price": "705.6699",
     "gamma_per_one_percent_move_oi": "-9623603185.89"},
]}

_GEX_SPY: dict[str, Any] = {"data": {
    "date": "2026-09-15", "time": "2026-09-15T15:28:10.000000Z", "source": "oi",
    "call_wall": "800", "gamma_flip": "762.53", "gamma_magnet": "760",
    "nearby_flips": ["762.53", "763.57"], "put_wall": "641",
}}
_GEX_QQQ: dict[str, Any] = {"data": {
    "date": "2026-09-15", "time": "2026-09-15T15:28:12.000000Z", "source": "oi",
    "call_wall": "720", "gamma_flip": "710.99", "gamma_magnet": "700", "put_wall": "584.78",
}}


def _term(dte: int, vol: str, expiry: str, ticker: str = "SPY", move: str = "1",
          perc: str = "0.01") -> dict[str, Any]:
    return {"date": "2026-09-15", "ticker": ticker, "volatility": vol, "expiry": expiry,
            "dte": dte, "implied_move": move, "implied_move_perc": perc}


_TERM_SPY: dict[str, Any] = {"data": [
    _term(0, "0.1440630820409955", "2026-09-15"),
    _term(2, "0.1821650647911526", "2026-09-17"),  # event hump, excluded
    _term(3, "0.1826171595289641", "2026-09-18"),
    _term(24, "0.1411492629261562", "2026-10-09"),
    _term(31, "0.142251103140261", "2026-10-16"),
    _term(66, "0.153561024244949", "2026-11-20"),
    _term(94, "0.1567835746493346", "2026-12-18"),
    _term(107, "0.1550593554882189", "2026-12-31"),
]}

_TERM_VIX: dict[str, Any] = {"data": [
    _term(1, "1.01053330293517", "2026-09-16", "VIX", "0.601772712995892", "0.03438701217119383"),
    _term(36, "0.719166446167389", "2026-10-21", "VIX", "2.855231326748495", "0.1631560758141997"),
    _term(64, "0.709368465794785", "2026-11-18", "VIX", "3.77843197749135", "0.2159103987137914"),
]}

_GREEK_SPY: dict[str, Any] = {"data": [
    {"date": "2025-10-02", "call_gamma": "5440909.4850", "put_gamma": "-4797183.4421"},
    {"date": "2025-10-03", "call_gamma": "5557994.7498", "put_gamma": "-4809600.9894"},
    {"date": "2025-10-06", "call_gamma": "4127677.9547", "put_gamma": "-4527138.3480"},
    {"date": "2026-09-11", "call_gamma": "3865650.1242", "put_gamma": "-4721346.5061"},
    {"date": "2026-09-14", "call_gamma": "2695976.8465", "put_gamma": "-5771237.5726"},
    {"date": "2026-09-15", "call_gamma": "3087662.2552", "put_gamma": "-6000555.5375"},
]}


def _paths() -> dict[str, dict[str, Any]]:
    return {
        rg.MARKET_TIDE_PATH: _TIDE,
        rg.SPOT_EXPOSURES_PATH.format(ticker="SPY"): _SPOT_SPY,
        rg.SPOT_EXPOSURES_PATH.format(ticker="QQQ"): _SPOT_QQQ,
        rg.GEX_LEVELS_PATH.format(ticker="SPY"): _GEX_SPY,
        rg.GEX_LEVELS_PATH.format(ticker="QQQ"): _GEX_QQQ,
        rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): _TERM_SPY,
        rg.TERM_STRUCTURE_PATH.format(ticker="VIX"): _TERM_VIX,
        rg.GREEK_EXPOSURE_PATH.format(ticker="SPY"): _GREEK_SPY,
        rg.GREEK_EXPOSURE_PATH.format(ticker="QQQ"): _GREEK_SPY,
    }


class _FakeClient:
    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._responses = responses if responses is not None else _paths()
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self._errors:
            raise self._errors[path]
        return self._responses.get(path, {"data": []})


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    rg.ensure_regime_tables(engine)
    return session_factory(engine)


async def _refresh(sessions: Any, settings: BoardSettings, client: _FakeClient,
                   now: datetime = _FETCH) -> rg.RegimeRefreshReport:
    report = await rg.refresh_regime(client, sessions, settings=settings, now=now)  # type: ignore[arg-type]
    await rg.refresh_gamma_history(client, sessions, settings=settings, now=now)  # type: ignore[arg-type]
    return report


def _band(sessions: Any, settings: BoardSettings, now: datetime) -> rg.RegimeBand:
    with sessions() as s:
        inputs = rg.load_regime_inputs(s, now=now)
    return rg.build_regime_band(inputs, settings=settings, now=now)


def _chip(band: rg.RegimeBand, key: str) -> rg.RegimeChip:
    return next(c for c in band.chips if c.key == key)


# ---------------------------------------------------------------------------
# fetch and store
# ---------------------------------------------------------------------------


def test_ensure_tables_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    rg.ensure_regime_tables(engine)
    rg.ensure_regime_tables(engine)
    assert "alfa_regime" in inspect(engine).get_table_names()


async def test_cycle_job_calls_the_seven_contract_endpoints(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient()
    report = await rg.refresh_regime(client, sessions, settings=settings, now=_FETCH)  # type: ignore[arg-type]
    assert client.calls == [
        ("/api/market/market-tide", {"interval_5m": "true"}),
        ("/api/stock/SPY/spot-exposures", None),
        ("/api/stock/QQQ/spot-exposures", None),
        ("/api/stock/SPY/gex-levels", {"source": "oi"}),
        ("/api/stock/QQQ/gex-levels", {"source": "oi"}),
        ("/api/stock/SPY/volatility/term-structure", None),
        ("/api/stock/VIX/volatility/term-structure", None),
    ]
    assert report.requests == 7
    assert len(report.stored) == 7
    assert not any("vix-term-structure" in path for path, _ in client.calls)


async def test_tide_keeps_the_last_complete_bucket(sessions: Any, settings: BoardSettings) -> None:
    await _refresh(sessions, settings, _FakeClient())
    with sessions() as s:
        inputs = rg.load_regime_inputs(s, now=_FETCH)
    (bucket,) = inputs.tide_buckets
    # 11:25 bucket is still filling at 11:28:30; 11:20-11:25 is the last complete one.
    assert bucket.bucket_start == datetime(2026, 9, 15, 15, 20, tzinfo=UTC)
    assert bucket.bucket_end == datetime(2026, 9, 15, 15, 25, tzinfo=UTC)
    assert bucket.net_premium == pytest.approx(-109857642.0 - 15568937.0)
    assert bucket.net_volume == -543763
    with sessions() as s:
        row = s.scalars(select(rg.AlfaRegime).where(rg.AlfaRegime.source == rg.SOURCE_TIDE)).one()
    assert row.source_time is not None


async def test_spot_exposures_use_regular_session_rows_only(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient())
    with sessions() as s:
        inputs = rg.load_regime_inputs(s, now=_FETCH)
    spy = next(g for g in inputs.gamma if g.ticker == "SPY")
    assert spy.gamma_oi == pytest.approx(-18896117834.97)
    assert spy.price == pytest.approx(757.38)
    assert spy.session_first_time == datetime(2026, 9, 15, 13, 32, 58, tzinfo=UTC)
    assert spy.session_first_gamma_oi == pytest.approx(-15846195965.93)


async def test_session_open_follows_eastern_time_in_winter(
    sessions: Any, settings: BoardSettings,
) -> None:
    winter = {"data": [
        {"time": "2026-01-15T14:00:00Z", "price": "600", "gamma_per_one_percent_move_oi": "5e9"},
        {"time": "2026-01-15T14:31:00Z", "price": "601", "gamma_per_one_percent_move_oi": "-2e9"},
        {"time": "2026-01-15T15:00:00Z", "price": "602", "gamma_per_one_percent_move_oi": "-3e9"},
    ]}
    now = datetime(2026, 1, 15, 15, 1, tzinfo=UTC)
    client = _FakeClient({rg.SPOT_EXPOSURES_PATH.format(ticker="SPY"): winter})
    await rg.refresh_spot_exposures(client, sessions, tickers=["SPY"], settings=settings, now=now)  # type: ignore[arg-type]
    with sessions() as s:
        (spy,) = rg.load_regime_inputs(s, now=now).gamma
    # 14:00Z is 09:00 EST (pre-market); the session opens at 14:30Z in winter.
    assert spy.session_first_time == datetime(2026, 1, 15, 14, 31, tzinfo=UTC)


async def test_curve_skips_event_hump_and_picks_nearest_anchor_dte(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient())
    with sessions() as s:
        inputs = rg.load_regime_inputs(s, now=_FETCH)
    assert inputs.curve is not None
    assert (inputs.curve.short_dte, inputs.curve.long_dte) == (31, 94)
    assert inputs.vix is not None
    assert inputs.vix.vix_spot == pytest.approx(17.50, abs=0.01)


async def test_curve_with_one_usable_expiry_is_no_data(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): {"data": [
        _term(1, "0.19", "2026-09-16"), _term(40, "0.15", "2026-10-25"),
    ]}})
    report = await rg.refresh_iv_term_structure(client, sessions, settings=settings, now=_FETCH)  # type: ignore[arg-type]
    assert report.no_data == (rg.SOURCE_CURVE,)


async def test_gamma_history_percentile_and_base_rate(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient())
    with sessions() as s:
        inputs = rg.load_regime_inputs(s, now=_FETCH)
    spy = next(h for h in inputs.history if h.ticker == "SPY")
    assert (spy.negative_days, spy.total_days) == (4, 6)
    assert spy.percentile == pytest.approx(2 / 6 * 100)
    assert spy.latest_net == pytest.approx(3087662.2552 - 6000555.5375)


async def test_history_is_append_only_and_replay_safe(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient())
    await _refresh(sessions, settings, _FakeClient())  # same fetch time: no duplicate, no error
    await _refresh(sessions, settings, _FakeClient(), now=_FETCH + timedelta(minutes=5))
    with sessions() as s:
        counts = dict(s.execute(
            select(rg.AlfaRegime.source, func.count()).group_by(rg.AlfaRegime.source),
        ).all())
    assert counts[rg.SOURCE_TIDE] == 2
    assert counts[rg.spot_source("SPY")] == 2
    assert sum(counts.values()) == 18


# ---------------------------------------------------------------------------
# UW errors
# ---------------------------------------------------------------------------


async def test_not_found_and_degraded_sources_do_not_stop_the_cycle(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient(errors={
        rg.MARKET_TIDE_PATH: UnusualWhalesNotFoundError("404", status_code=404),
        rg.SPOT_EXPOSURES_PATH.format(ticker="SPY"): UnusualWhalesRateLimitError("429"),
        rg.GEX_LEVELS_PATH.format(ticker="QQQ"): UnusualWhalesTransientError("503"),
        rg.TERM_STRUCTURE_PATH.format(ticker="VIX"): CircuitBreakerOpenError("open"),
    })
    report = await rg.refresh_regime(client, sessions, settings=settings, now=_FETCH)  # type: ignore[arg-type]
    assert len(client.calls) == 7
    assert report.no_data == (rg.SOURCE_TIDE,)
    assert report.degraded == (rg.spot_source("SPY"), rg.gex_source("QQQ"), rg.SOURCE_VIX)
    assert len(report.stored) == 3


@pytest.mark.parametrize(
    "error", [UnusualWhalesDailyLimitError("daily_request_limit"), UnusualWhalesAuthError("403")],
)
async def test_daily_limit_and_auth_propagate(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient(errors={rg.MARKET_TIDE_PATH: error})
    with pytest.raises(type(error)):
        await rg.refresh_regime(client, sessions, settings=settings, now=_FETCH)  # type: ignore[arg-type]
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# band
# ---------------------------------------------------------------------------


async def test_band_sentence_chips_and_tripwires(sessions: Any, settings: BoardSettings) -> None:
    await _refresh(sessions, settings, _FakeClient())
    band = _band(sessions, settings, _FETCH + timedelta(seconds=30))
    assert band.sentence_parts == (
        "Piyasa akışı aşağı yönlü: net prim -$125.4M, net hacim -544k kontrat (11:20 ET kovası)",
        "SPY kısa gamma: -$18.9B/%1",
        "QQQ kısa gamma: -$9.6B/%1",
        "SPY IV vadesi contango (IV31G %14.2 · IV94G %15.7)",
        "VIX ≈ 17.5 (türetilmiş)",
    )
    assert _chip(band, "gamma:SPY").text == (
        "SPY kısa gamma: -$18.9B/%1 · 1 yıllık yüzdelik %33 (15.09 itibarıyla)"
        " · son 1 yılın 4/6 gününde kısa gamma"
    )
    assert _chip(band, "flip:SPY").text == (
        "SPY en yakın strike işaret değişimi 762.53 (spottan %+0.7)"
    )
    assert _chip(band, "vix_curve").text == (
        "VIX vade yapısı: kapsam-dışı (volatilite eklentisi yok)"
    )
    assert _chip(band, "vix_curve").known is False
    assert band.tripwire_head == "fikrimi ne değiştirir"
    gamma, tide, curve = band.tripwires
    assert gamma.fired is False and gamma.status_text == "tetiklenmedi"
    assert gamma.text == (
        "SPY veya QQQ gamma ölü bandı (±$1.0B) seans başına göre öbür tarafa geçerse: "
        "SPY -$18.9B (seans başı -$15.8B), QQQ -$9.6B (seans başı -$9.6B)"
    )
    assert tide.fired is False
    assert tide.text == (
        "Akış yön değiştirip 3 tamamlanmış kova boyunca kalırsa (ölü bant ±$25.0M): "
        "şu an -$125.4M, ters yönde 0/3 kova"
    )
    assert curve.fired is False
    assert curve.text == "SPY IV31G ≥ IV94G + 0.5 vol puanı olursa: şu an %14.2 / %15.7"


async def test_stale_sources_read_unknown(sessions: Any, settings: BoardSettings) -> None:
    await _refresh(sessions, settings, _FakeClient())
    later = _FETCH + timedelta(seconds=settings.regime.max_source_age_seconds + 60)
    band = _band(sessions, settings, later)
    assert band.sentence_parts == (
        "Piyasa akışı: bilinmiyor", "SPY gamma: bilinmiyor", "QQQ gamma: bilinmiyor",
        "SPY IV vadesi: bilinmiyor", "VIX: bilinmiyor",
    )
    assert all(t.fired is None and t.status_text == "bilinmiyor" for t in band.tripwires)
    assert _chip(band, "vix_curve").state == "out_of_scope"
    assert not any(c.known for c in band.chips)


async def test_empty_database_reads_unknown(sessions: Any, settings: BoardSettings) -> None:
    band = _band(sessions, settings, _FETCH)
    assert all(not c.known for c in band.chips)
    assert [t.fired for t in band.tripwires] == [None, None, None]


async def test_gamma_tripwire_fires_on_crossing_from_session_start(
    sessions: Any, settings: BoardSettings,
) -> None:
    crossed = {"data": [
        {"time": "2026-09-15T13:31:00Z", "price": "760", "gamma_per_one_percent_move_oi": "2e9"},
        {"time": "2026-09-15T15:28:10Z", "price": "757", "gamma_per_one_percent_move_oi": "-3e9"},
    ]}
    client = _FakeClient({**_paths(), rg.SPOT_EXPOSURES_PATH.format(ticker="SPY"): crossed})
    await _refresh(sessions, settings, client)
    gamma = _band(sessions, settings, _FETCH).tripwires[0]
    assert gamma.fired is True and gamma.status_text == "tetiklendi"


async def test_gamma_tripwire_is_unknown_when_one_index_is_missing(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({**_paths(), rg.SPOT_EXPOSURES_PATH.format(ticker="QQQ"): {"data": []}})
    await _refresh(sessions, settings, client)
    band = _band(sessions, settings, _FETCH)
    assert band.tripwires[0].fired is None
    assert "QQQ bilinmiyor" in band.tripwires[0].text


def _tide_rows(nets: list[float]) -> dict[str, Any]:
    start = datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
    return {"data": [
        {"timestamp": (start + timedelta(minutes=5 * i)).isoformat(), "date": "2026-09-15",
         "net_call_premium": str(net), "net_put_premium": "0", "net_volume": 1000}
        for i, net in enumerate(nets)
    ]}


async def test_tide_tripwire_counts_persistence_from_history(
    sessions: Any, settings: BoardSettings,
) -> None:
    nets = [60e6, 70e6, 10e6, -40e6, -50e6, -45e6, 0.0]  # last row is still filling
    start = datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
    for k in range(1, len(nets)):
        fetch = start + timedelta(minutes=5 * k, seconds=20)
        client = _FakeClient({rg.MARKET_TIDE_PATH: _tide_rows(nets[: k + 1])})
        await rg.refresh_market_tide(client, sessions, settings=settings, now=fetch)  # type: ignore[arg-type]
        band = _band(sessions, settings, fetch)
        tide = band.tripwires[1]
        if k < 6:
            assert tide.fired is False, k
    assert tide.fired is True
    assert "ters yönde 3/3 kova" in tide.text
    with sessions() as s:
        buckets = rg.load_regime_inputs(s, now=fetch).tide_buckets
    assert rg.tide_reversal(buckets, settings.regime.tide_deadband_usd) == (-1, 3, 1)


async def test_curve_inversion_fires_the_tripwire(sessions: Any, settings: BoardSettings) -> None:
    inverted = {"data": [_term(30, "0.20", "2026-10-15"), _term(90, "0.15", "2026-12-14")]}
    client = _FakeClient({**_paths(), rg.TERM_STRUCTURE_PATH.format(ticker="SPY"): inverted})
    await _refresh(sessions, settings, client)
    band = _band(sessions, settings, _FETCH)
    assert _chip(band, "curve").state == "inverted"
    assert band.tripwires[2].fired is True


async def test_every_generated_regime_string_is_clean(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient())
    texts = [
        rg.TRIPWIRE_HEAD, rg.UNKNOWN, rg.VIX_CURVE_OUT_OF_SCOPE, rg.VIX_SPOT_TEMPLATE,
        rg.BASE_RATE_TEMPLATE, rg.FLIP_LABEL, rg.CURVE_LABEL, rg.TIDE_LABEL, rg.TIDE_TEMPLATE,
        rg.GAMMA_TEMPLATE, rg.PERCENTILE_TEMPLATE, rg.FLIP_TEMPLATE, rg.CURVE_TEMPLATE,
        rg.SOURCE_UNKNOWN_TEMPLATE, rg.TRIPWIRE_GAMMA_TEMPLATE, rg.TRIPWIRE_GAMMA_VALUE,
        rg.TRIPWIRE_TIDE_TEMPLATE, rg.TRIPWIRE_CURVE_TEMPLATE,
        *rg.TIDE_WORDS.values(), *rg.GAMMA_WORDS.values(), *rg.CURVE_WORDS.values(),
        *rg.TRIPWIRE_STATUS.values(),
    ]
    for offset in (0, 30, 3600):
        band = _band(sessions, settings, _FETCH + timedelta(seconds=offset))
        texts.extend([band.sentence, *band.sentence_parts, *(c.text for c in band.chips)])
        texts.extend(t.text for t in band.tripwires)
    for text in texts:
        assert ensure_clean(text) == text
    assert "olasılık" not in " ".join(texts)
