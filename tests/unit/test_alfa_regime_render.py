"""Phase 5.2.B5b: the regime band on the rendered board (contract §6 B5).

The band's data layer (B5a) is seeded by running ``refresh_regime`` and
``refresh_gamma_history`` against the trimmed live payloads of
``test_board_regime``, then the board is rendered with the page clock pinned
just after that fetch.

Pins:
  - one sentence, then the chips: SPY and QQQ gamma with their one-year
    percentile and base rate, the nearest strike sign change with its distance
    from spot, the SPY IV term-structure shape, the out-of-scope VIX futures
    curve and the derived VIX spot;
  - every chip carries the age of the reading behind it;
  - an unknown or out-of-scope chip renders dashed and dimmed, and a stale
    source reads ``bilinmiyor``;
  - the three tripwires render under ``fikrimi ne değiştirir`` with their
    current values, cutoffs and status;
  - R-IV1 renders with the IV term-structure chip;
  - the band never enters the evidence counts, the strength label, the
    clean-candidate rule or the counter-argument choice;
  - R-WD1 over the page, and zero Unusual Whales calls.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import regime as rg
from webapp.board.alfa_page import build_alfa_page, build_regime_view, read_regime_inputs
from webapp.board.copy_tr import IV_NOT_SELL_VOL
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _seed
from tests.unit.test_board_regime import _FETCH, _FakeClient
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _FETCH + timedelta(seconds=30)  # 30 s after the regime fetch
_STALE = _FETCH + timedelta(seconds=_SETTINGS.regime.max_source_age_seconds + 60)
_BAND = re.compile(r"<section[^>]*data-regime>(.*?)</section>", re.S)
_ROWS = (
    _Row("r1", "AAA", "call", "at_ask", "300000", 0.4321, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
)


async def _seed_regime(url: str) -> None:
    engine = make_engine(url)
    try:
        rg.ensure_regime_tables(engine)
        sessions = session_factory(engine)
        client = _FakeClient()
        await rg.refresh_regime(client, sessions, settings=_SETTINGS, now=_FETCH)  # type: ignore[arg-type]
        await rg.refresh_gamma_history(client, sessions, settings=_SETTINGS, now=_FETCH)  # type: ignore[arg-type]
    finally:
        engine.dispose()


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'regime.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    await _seed_regime(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _band(body: str) -> str:
    found = _BAND.search(body)
    assert found is not None, "the regime band did not render"
    return found.group(1)


def _chip(band: str, key: str) -> str:
    found = re.search(rf'data-regime-chip="{re.escape(key)}" data-regime-state="([^"]*)">(.*?)</span>', band, re.S)
    assert found is not None, key
    return html.unescape(re.sub(r"<[^>]+>", "", found.group(2)))


def _chip_classes(band: str, key: str) -> str:
    found = re.search(rf'<span class="([^"]*)"\s+data-regime-chip="{re.escape(key)}"', band)
    assert found is not None, key
    return found.group(1)


# ---------------------------------------------------------------------------
# The band
# ---------------------------------------------------------------------------


def test_the_band_renders_one_sentence(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    sentence = re.search(r"data-regime-sentence>([^<]*)<", band)
    assert sentence is not None
    assert html.unescape(sentence.group(1)) == " · ".join((
        "Piyasa akışı aşağı yönlü: net prim -$125.4M, net hacim -544k kontrat (11:20 ET kovası)",
        "SPY kısa gamma: -$18.9B/%1",
        "QQQ kısa gamma: -$9.6B/%1",
        "SPY IV vadesi contango (IV31G %14.2 · IV94G %15.7)",
        "VIX ≈ 17.5 (türetilmiş)",
    ))


def test_the_gamma_chips_carry_the_percentile_and_the_base_rate(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    spy = _chip(band, "gamma:SPY")
    assert spy.startswith("SPY kısa gamma: -$18.9B/%1 · 1 yıllık yüzdelik %33 (15.09 itibarıyla)")
    assert "son 1 yılın 4/6 gününde kısa gamma" in spy
    assert "QQQ kısa gamma" in _chip(band, "gamma:QQQ")


def test_the_flip_is_labelled_and_carries_its_distance_from_spot(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    assert _chip(band, "flip:SPY").startswith(
        "SPY en yakın strike işaret değişimi 762.53 (spottan %+0.7)",
    )


def test_the_vix_curve_is_out_of_scope_and_the_spot_is_marked_derived(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    assert _chip(band, "vix_curve").startswith("VIX vade yapısı: kapsam-dışı (volatilite eklentisi yok)")
    assert "border-dashed" in _chip_classes(band, "vix_curve")
    assert _chip(band, "vix_spot").startswith("VIX ≈ 17.5 (türetilmiş)")


def test_the_iv_term_chip_renders_with_the_r_iv1_sentence(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    assert _chip(band, "curve").startswith("SPY IV vadesi contango (IV31G %14.2 · IV94G %15.7)")
    note = re.search(r"data-regime-iv1>([^<]*)<", band)
    assert note is not None
    assert html.unescape(note.group(1)) == IV_NOT_SELL_VOL


def test_every_chip_shows_the_age_of_its_source(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    ages = [html.unescape(a) for a in re.findall(r"data-regime-age>([^<]*)<", band)]
    assert ages, "no source age rendered"
    assert all(re.fullmatch(r"\d+ sn önce alındı", age) for age in ages), ages
    # Every chip with a source behind it carries one; the out-of-scope VIX curve has none.
    assert len(ages) == 7  # tide, SPY/QQQ gamma, SPY/QQQ flip, the IV curve and the VIX spot


def test_the_tripwires_render_with_their_values_and_status(client: TestClient) -> None:
    band = _band(client.get("/alfa").text)
    head = re.search(r"data-tripwire-head>([^<]*)<", band)
    assert head is not None
    assert html.unescape(head.group(1)) == "fikrimi ne değiştirir"
    wires = re.findall(r'data-tripwire="(\w+)" data-tripwire-status="(\w+)">([^<]*)<', band)
    assert [key for key, _status, _text in wires] == ["gamma", "tide", "curve"]
    assert {status for _key, status, _text in wires} == {"not_fired"}
    gamma_text = html.unescape(wires[0][2])
    assert gamma_text.startswith("SPY veya QQQ gamma ölü bandı (±$1.0B)")
    assert gamma_text.endswith("tetiklenmedi")
    assert "0.5 vol puanı olursa" in html.unescape(wires[2][2])


# ---------------------------------------------------------------------------
# Staleness and isolation
# ---------------------------------------------------------------------------


async def test_a_stale_source_reads_unknown_and_is_dimmed(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'stale.db'}"
    await _seed_regime(url)
    engine = make_engine(url)
    try:
        view = build_regime_view(
            read_regime_inputs(engine, now=_STALE), settings=_SETTINGS, now=_STALE,
        )
    finally:
        engine.dispose()
    assert [chip.known for chip in view.band.chips] == [False] * len(view.band.chips)
    assert view.band.sentence_parts[0] == "Piyasa akışı: bilinmiyor"
    assert all(wire.fired is None for wire in view.band.tripwires)


def _prints() -> list[BoardPrint]:
    out: list[BoardPrint] = []
    for i, ticker in enumerate(("AAA", "BBB")):
        signal = BacktestStore().add(
            EnrichedEvent(
                print=build_print(
                    event_id=ticker, ts=_TS + timedelta(seconds=i), ticker=ticker,
                    option_type="call", strike="100", dte=3, premium="100000",
                ),
                combined_score_post_penalty=0.42,
            ),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
        out.append(BoardPrint(run_id="live-2026-09-15", event_id=ticker, signal=signal,
                              meta=PrintMetaView("at_ask", None)))
    return out


async def test_the_band_never_enters_the_counts_or_the_counter_argument(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'isolation.db'}"
    await _seed_regime(url)
    engine = make_engine(url)
    try:
        prints = _prints()
        plain = build_alfa_page(prints, _SETTINGS, now=_NOW)
        with_band = build_alfa_page(
            prints, _SETTINGS, now=_NOW,
            regime_source=lambda moment: read_regime_inputs(engine, now=moment),
        )
    finally:
        engine.dispose()
    assert plain.regime is None
    assert with_band.regime is not None
    assert with_band.clean_candidate_count == plain.clean_candidate_count
    def _readings(page: Any) -> list[Any]:
        return [
            (v.row.ticker, v.evidence.counts, v.strength_key, v.clean_candidate,
             v.narrative.counter, v.narrative.checked)
            for v in page.views
        ]
    assert _readings(with_band) == _readings(plain)


async def test_a_failed_regime_read_still_renders_the_band_as_unknown() -> None:
    def _broken(_moment: datetime) -> Any:
        msg = "alfa_regime unavailable"
        raise RuntimeError(msg)

    page = build_alfa_page([], _SETTINGS, now=_NOW, regime_source=_broken)
    assert page.regime is not None
    assert not any(chip.known for chip in page.regime.band.chips)


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


def test_the_band_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/alfa", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_band_renders_outside_every_row(client: TestClient) -> None:
    body = client.get("/alfa").text
    assert body.index("data-regime>") < body.index("<article")
    for article in body.split("<article")[1:]:
        assert "data-regime>" not in article.split("</article>")[0]


def test_the_band_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/alfa")
    assert response.status_code == 200
    assert "data-regime-sentence" in response.text
    assert calls == []
