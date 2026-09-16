"""Phase 5.2.A-fix5: render hygiene near-misses (review FA-05, FA-08, FA-09).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-EV1 (no number
presented as an expected value), R-EV2 (score values only in the audit block)
and R-CO2 (every quote shows its age).

Pins:
  - FA-05: a legacy family's row-face tooltip names the persisted record, never
    its score value; the value stays in ``FamilyRead.source`` (audit metadata)
    and in the Denetim block's inputs;
  - FA-08: the vol board labels ``atm_iv · sqrt(30/365)`` as an IV-implied
    1σ move; no visible text on ``/`` or ``/alfa`` says "expected";
  - FA-09: ``/gamma`` shows each spot price with its as-of date;
  - the new legacy tooltips pass ``ensure_clean``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board.alfa_page import load_live_legacy_scores
from webapp.board.evidence import LEGACY_HOVER, legacy_family
from webapp.board.honesty import ensure_clean

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _reset, _rows
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_LEGACY = load_live_legacy_scores(_REPO / "profiles" / "v5_default.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)
_COMMENTS = re.compile(r"<!--.*?-->", re.S)


def _legacy_event() -> EnrichedEvent:
    pr = build_print(event_id="leg1", ts=_TS, ticker="LEG", option_type="call", strike="100", dte=3, premium="100000")
    return EnrichedEvent(print=pr, price_confirmation_score=1.0, sector_confirmation_score=0.7)


def _decision() -> tuple[LabelDecision, PositionSize]:
    return (
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _visible(body: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", _COMMENTS.sub("", body)))
    return re.sub(r"\s+", " ", text)


def test_legacy_tooltip_names_the_record_not_the_value() -> None:
    signal = BacktestStore().add(_legacy_event(), *_decision())
    for family, field in (("price_confirmation", "price_confirmation_score=1.0"),
                          ("sector", "sector_confirmation_score=0.7")):
        read = legacy_family(family, signal, sold=False, scores=_LEGACY)
        assert read.state == "supporting", family
        assert read.source == field  # audit metadata keeps the persisted value
        assert read.hover == LEGACY_HOVER[family]
        assert read.hover is not None
        assert not re.search(r"\d\.\d|=", read.hover), read.hover  # no score value (M23/M25 names are fine)
    for text in LEGACY_HOVER.values():
        assert ensure_clean(text) == text


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'hygiene.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    _reset(m)
    store = SqliteBacktestStore(url, flush_threshold=2)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        store.add(_legacy_event(), *_decision())
    finally:
        store.close()
    from webapp.gamma import GammaRepo

    GammaRepo().upsert("TSLA", "2026-09-15", {
        "spot": 250.0, "net_gex": 1.0, "flip": 240.0, "call_wall": 260.0, "put_wall": 240.0,
        "atm_iv": 0.4, "iv_pct": 0.9,
    })
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def test_legacy_score_values_stay_inside_the_audit_block(client: TestClient) -> None:
    body = client.get("/", params={"gate": "off"}).text
    row = _rows(body)["LEG"]
    outside = _AUDIT.sub("", row)
    titles = [html.unescape(t) for t in re.findall(r'title="([^"]*)"', outside)]
    assert any(t == LEGACY_HOVER["price_confirmation"] for t in titles)
    for title in titles:
        assert "score" not in title, title
        assert "=1.0" not in title and "=0.7" not in title, title
    (audit,) = _AUDIT.findall(row)
    assert "Fiyat teyidi: 1.00" in _visible(audit)
    assert "Sektör: 0.70" in _visible(audit)


@pytest.mark.parametrize("path", ["/", "/alfa"])
def test_vol_board_move_is_labelled_as_implied_not_expected(client: TestClient, path: str) -> None:
    text = _visible(client.get(path).text)
    assert "IV-implied 1\u03c3 move" in text
    assert "expected" not in text.lower()
    assert "beklenen" not in text.lower()


def test_gamma_spot_shows_its_as_of_date(client: TestClient) -> None:
    body = client.get("/gamma").text
    cell = re.search(r"\$250\.00\s*<div[^>]*data-spot-as-of>([^<]*)</div>", body)
    assert cell is not None
    assert cell.group(1) == "as of 2026-09-15"
