"""Phase 5.2.B1: the position-size cell on the rendered board (contract §6 B1).

The seeded sqlite run of ``test_board_honesty`` is reused, with rows chosen for
the size states: a cheap tradable contract (1 lot), an expensive one (0 lots),
and a row with no quote (bilinmiyor).

Pins:
  - the cell shows 1 lot in dollars and as a share of capital, and the
    contract's risk-bucket line;
  - 0 lots renders its frozen note instead of being hidden;
  - a row without an executable quote reads ``bilinmiyor`` and still shows the
    chip's quote age where there is one;
  - ``(varsayılan değer)`` is on the cell while the owner values are
    unconfirmed, and gone once they are confirmed;
  - the max_r disclosure appears only inside the ``data-audit`` block (R-EV2),
    and the 0-1 score never leaves it;
  - R-WD1 over the rendered page, and zero Unusual Whales calls.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings
from webapp.board.sizing import SIZE_COPY

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)

# CHP: 1 lot at $0.41 fits the $50 risk amount. EXP: 1 lot at $2.05 does not. NOQ: no quote.
# The scores are chosen not to collide with any rendered size number (the R-EV2 check below).
_ROWS = (
    _Row("b1", "CHP", "call", "at_ask", "300000", 0.6137, _CONFIRMING, quote=(0.39, 0.41)),
    _Row("b2", "EXP", "call", "at_ask", "200000", 0.7248, _CONFIRMING, quote=(1.95, 2.05)),
    _Row("b3", "NOQ", "call", "at_ask", "100000", 0.8319, _CONFIRMING),
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'size.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _TS + timedelta(hours=1))
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(row_html: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", row_html)
    return None if found is None else html.unescape(found.group(1))


def test_the_size_cell_shows_one_lot_and_the_risk_bucket_line(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert set(rows) == {"CHP", "EXP", "NOQ"}
    assert _cell(rows["CHP"], "data-size-lot") == "1 lot $41 · sermayenin %0.41 kadarı"
    assert _cell(rows["CHP"], "data-size-bucket") == (
        "Profil risk kovası: STANDARD_UOA, max_r 0.5 → 0.5 × R = $50 → 1 lot"  # noqa: RUF001
    )
    assert 'data-size="known"' in rows["CHP"]
    assert "data-size-zero" not in rows["CHP"]


def test_zero_lots_is_shown_not_hidden(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["EXP"]
    assert _cell(row, "data-size-lot") == "1 lot $205 · sermayenin %2.05 kadarı"
    assert _cell(row, "data-size-bucket") == (
        "Profil risk kovası: STANDARD_UOA, max_r 0.5 → 0.5 × R = $50 → 0 lot"  # noqa: RUF001
    )
    assert _cell(row, "data-size-zero") == SIZE_COPY["zero_lots"]


def test_a_row_without_a_quote_reads_unknown(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["NOQ"]
    assert _cell(row, "data-size-lot") == SIZE_COPY["lot_unknown"]
    assert _cell(row, "data-size-bucket") == (
        "Profil risk kovası: STANDARD_UOA, max_r 0.5 → 0.5 × R = $50 → lot sayısı bilinmiyor"  # noqa: RUF001
    )
    assert 'data-size="unknown"' in row
    assert "data-size-quote-age" not in row  # no quote row at all: no age to show


def test_a_priced_row_keeps_its_quote_age_on_the_size_cell(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["CHP"]
    age = _cell(row, "data-size-quote-age")
    assert age is not None
    assert re.fullmatch(r"kotasyon \d+ sn önce alındı", age)


def test_the_size_cell_marks_a_default_only_while_the_owner_has_not_confirmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.3.5 recorded O3 in the profile, so this test was inverted, not dropped.

    Confirmation is now the profile's own state, so asserting it here would prove
    nothing. What still needs pinning is the disclosure: an unconfirmed capital
    figure must say so on the cell that spends it.
    """
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["CHP"], "data-size-lot") is not None
    assert _cell(rows["CHP"], "data-size-default") is None

    import webapp.main as m

    unconfirmed = _SETTINGS.model_copy(
        update={"sizing": _SETTINGS.sizing.model_copy(update={"values_confirmed_by_owner": False})},
    )
    monkeypatch.setattr(m, "_board_settings", lambda: unconfirmed)
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["CHP"], "data-size-default") == "(varsayılan değer)"


def test_the_max_r_disclosure_lives_only_in_the_audit_block(client: TestClient) -> None:
    body = client.get("/", params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    assert "max_r satırın" not in html.unescape(outside)
    for ticker, row in _rows(body).items():
        (audit,) = _AUDIT.findall(row)
        assert "max_r satırın STANDARD_UOA etiketinden gelir" in html.unescape(audit), ticker
    for spec in _ROWS:  # R-EV2: the size cell never leaks a score
        assert f"{spec.score:.2f}" not in outside, spec.ticker


def test_the_size_cell_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_size_render_makes_zero_unusual_whales_calls(
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
    assert "data-size-lot" in response.text
    assert calls == []
