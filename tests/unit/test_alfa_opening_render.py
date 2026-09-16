"""Phase 5.2.B4b: the opening/closing reading and the catalyst chip on the board.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B4, §9 (the Açık
pozisyon row) and §5 A5 (counter-argument priority 4); decisions P10 and P13.

The seeded sqlite run of ``test_board_honesty`` is reused, with one row per
confirmation state plus a row whose expiry window holds an earnings report.

Pins:
  - the four contract labels render on the row, byte for byte;
  - the Açık pozisyon evidence family follows the same confirmation;
  - an unconfirmed or out-of-scope reading renders dashed and dimmed, never
    clean (R-UN1);
  - a never-fetched catalyst source reads ``bilinmiyor``, and a catalyst inside
    the window becomes the row's mandatory counter-argument;
  - the fallback's checked list names the catalyst check;
  - the audit block carries the "M22 may differ" note, and the combined score
    stays inside it (R-EV2);
  - R-WD1 over the rendered page, and zero Unusual Whales calls.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.catalysts import (
    M22_MAY_DIFFER,
    AlfaCatalyst,
    AlfaCatalystFetch,
    ensure_catalyst_tables,
)
from webapp.board.db import make_engine
from webapp.board.honesty import forbidden_words
from webapp.board.oi_confirm import STATUS_LABELS, AlfaOiConfirm, ensure_oi_confirm_tables

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_TRADE_DATE = date(2026, 9, 15)
_EXPIRY = date(2026, 9, 18)
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)

# One row per confirmation state, plus KAT for the catalyst window. Every row is
# İŞLENİR at a 5% spread, so cost is never the counter-argument.
_ROWS = (
    _Row("o1", "ACI", "call", "at_ask", "500000", 0.4211, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
    _Row("o2", "KAP", "call", "at_ask", "400000", 0.5322, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
    _Row("o3", "BEK", "call", "at_ask", "300000", 0.6433, _CONFIRMING, quote=(0.39, 0.41)),
    _Row("o4", "VAD", "call", "at_ask", "200000", 0.7544, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
    _Row("o5", "KAT", "call", "at_ask", "100000", 0.8655, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
)
_STATUSES = {"ACI": "acilis", "KAP": "kapanis", "VAD": "kapsam_disi"}


def _seed_confirmations(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_oi_confirm_tables(engine)
        ensure_catalyst_tables(engine)
        with Session(engine) as session:
            for row in _ROWS:
                status = _STATUSES.get(row.ticker)
                if status is None:
                    continue  # BEK and KAT have no confirmation row yet
                session.add(AlfaOiConfirm(
                    option_symbol=row.symbol, trade_date=_TRADE_DATE, ticker=row.ticker,
                    option_type="call", strike=100.0, expiry=_EXPIRY, flagged_size=40,
                    status=status, created_at=_NOW,
                    oi_t=97, oi_t1=140 if status == "acilis" else 40,
                    t1_date=date(2026, 9, 16), delta_oi=43 if status == "acilis" else -57,
                ))
            # KAT reports inside the window: 2026-09-17 after the close, in ET.
            session.add(AlfaCatalyst(
                ticker="KAT", kind="earnings", when_key="2026-09-17", title="Q3",
                starts_at=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 4, 0, tzinfo=UTC),
                precision="day", timing="postmarket", estimated=False, detail=None,
                fetched_at=_NOW,
            ))
            session.add(AlfaCatalystFetch(
                source="earnings", ticker="KAT", last_attempt_at=_NOW, last_status="ok",
                last_success_at=_NOW,
            ))
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'opening.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    _seed_confirmations(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(row_html: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", row_html)
    return None if found is None else html.unescape(found.group(1))


def _family(row_html: str, family: str) -> str:
    found = re.search(rf'data-family="{family}" data-family-state="(\w+)"[^>]*>([^<]*)<', row_html)
    assert found is not None, family
    return html.unescape(found.group(2))


# ---------------------------------------------------------------------------
# The row line
# ---------------------------------------------------------------------------


def test_each_confirmation_state_renders_its_frozen_label(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["ACI"], "data-oi-label") == STATUS_LABELS["acilis"] == "açılış (T+1 OI teyitli)"
    assert _cell(rows["KAP"], "data-oi-label") == STATUS_LABELS["kapanis"] == "kapanış (T+1 OI düştü)"
    assert _cell(rows["BEK"], "data-oi-label") == "henüz doğrulanmadı"
    assert _cell(rows["VAD"], "data-oi-label") == "kapsam-dışı (T+1'den önce vade)"
    assert 'data-opening="acilis"' in rows["ACI"]
    assert 'data-opening="yok"' in rows["BEK"]


def test_the_open_interest_family_follows_the_confirmation(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _family(rows["ACI"], "open_interest") == "Açık pozisyon: lehte"
    assert _family(rows["KAP"], "open_interest") == "Açık pozisyon: aleyhte"
    assert _family(rows["BEK"], "open_interest").startswith("Açık pozisyon: bilinmiyor (T+1 bekleniyor)")
    assert _family(rows["VAD"], "open_interest").startswith("Açık pozisyon: kapsam-dışı")


def test_an_unconfirmed_or_out_of_scope_reading_is_dashed_and_dimmed(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    for ticker in ("BEK", "VAD"):
        label = re.search(r'<span class="([^"]*)"\s+data-oi-label>', rows[ticker])
        assert label is not None, ticker
        assert "border-dashed" in label.group(1), ticker
    for ticker in ("ACI", "KAP"):
        label = re.search(r'<span class="([^"]*)"\s+data-oi-label>', rows[ticker])
        assert label is not None, ticker
        assert "border-dashed" not in label.group(1), ticker


# ---------------------------------------------------------------------------
# The catalyst chip
# ---------------------------------------------------------------------------


def test_a_catalyst_inside_the_window_renders_and_a_never_fetched_source_reads_unknown(
    client: TestClient,
) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    kat = _cell(rows["KAT"], 'data-catalyst="var"')
    assert kat is not None
    assert kat.startswith("Vade içinde katalizör: Kazanç: 17.09 kapanış sonrası")
    assert "FDA: bilinmiyor" in kat and "Makro: bilinmiyor" in kat
    other = _cell(rows["ACI"], 'data-catalyst="yok"')
    assert other == "Vade içinde katalizör: Kazanç: bilinmiyor · FDA: bilinmiyor · Makro: bilinmiyor"


def test_a_catalyst_in_the_window_is_the_mandatory_counter_argument(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["KAT"], "<p data-counter") == "AMA vade içinde katalizör var: Kazanç."


def test_the_fallback_checked_list_names_the_catalyst_check(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["ACI"]
    counter = _cell(row, "<p data-counter")
    assert counter == "Bariz bir karşı argüman bulunamadı — bu bir onay değildir"
    checked = _cell(row, "<p data-checked")
    assert checked is not None
    assert "vade içi katalizör (0)" in checked


# ---------------------------------------------------------------------------
# Audit and honesty
# ---------------------------------------------------------------------------


def test_the_m22_note_lives_only_in_the_audit_block(client: TestClient) -> None:
    body = client.get("/", params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    assert M22_MAY_DIFFER not in html.unescape(outside)
    for ticker, row in _rows(body).items():
        (audit,) = _AUDIT.findall(row)
        assert M22_MAY_DIFFER in html.unescape(audit), ticker


def test_the_score_never_leaves_the_audit_block(client: TestClient) -> None:
    body = client.get("/", params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    for spec in _ROWS:
        assert f"{spec.score:.2f}" not in outside, spec.ticker


def test_the_opening_line_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_opening_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/", params={"gate": "off"})
    assert response.status_code == 200
    assert "data-oi-label" in response.text
    assert calls == []
