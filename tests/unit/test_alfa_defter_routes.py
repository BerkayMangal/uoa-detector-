"""Phase 5.2.C2a: ``GET /defter`` — the pass ledger page.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2) and §6.

The board is seeded with the shared harness (SPY yukarı and NVDA aşağı), the
cards are made by pressing the real buttons, and the page is read back through
a ``TestClient``.

Pins:
  - a recorded pass and a recorded log both appear, newest first, each linking
    to its own card page;
  - what the page shows is the card's FROZEN face, not a fresh read of today's
    board;
  - the decision and ticker filters, and an unknown filter value that lists
    everything rather than nothing;
  - outcomes render from ``alfa_outcome``; a horizon with no row reads
    ``henüz hesaplanmadı`` and never a zero;
  - the cap is disclosed and its value comes from the board profile;
  - the count gate at ``fills.min_n_for_stats``, rendered, in both directions;
  - a card whose snapshot cannot be read still renders (``bilinmiyor``), and a
    failed read says plainly that nothing was deleted;
  - the route makes ZERO Unusual Whales calls;
  - no forbidden word reaches the page, and the auth enumeration covers the new
    route without being edited.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from webapp.board import cards, outcomes
from webapp.board.db import make_engine
from webapp.board.decision_ledger import card_face
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings

from tests.unit._alfa_card_harness import card_id_from, press, reset_singletons, seed
from tests.unit._webapp_auth import authed_client
from tests.unit.test_webapp_auth import _CASES
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_MIN_N = _SETTINGS.fills.min_n_for_stats  # D8: profile-driven, never a literal
_HORIZONS = _SETTINGS.outcomes.horizons_trading_days
_CAP = _SETTINGS.ledger.max_cards_per_page

_CARD_RE = re.compile(
    r'<article[^>]*?data-card="([^"]+)"[^>]*?data-card-decision="([^"]+)"'
    r'[^>]*?data-card-ticker="([^"]+)"[^>]*?>(.*?)</article>',
    re.S,
)
_HORIZON_RE = re.compile(r'data-horizon="(\d+)">(.*?)</div>', re.S)


def _reset(module: ModuleType) -> None:
    """Every cached repository, including the two the shared harness predates."""
    reset_singletons(module)
    module._FILLS = None
    module._OUTCOMES = None


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType, str]]:
    url = f"sqlite:///{tmp_path / 'defter.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)  # no price fetch anywhere
    import webapp.main as m

    _reset(m)
    seed(url)
    yield authed_client(m.app, monkeypatch), m, url
    _reset(m)


def _defter(client: TestClient, **params: str) -> str:
    response = client.get("/defter", params=params)
    assert response.status_code == 200
    return response.text


def _blocks(body: str) -> dict[str, str]:
    """Rendered card id → its inner HTML, in page order."""
    return {card_id: inner for card_id, _decision, _ticker, inner in _CARD_RE.findall(body)}


def _order(body: str) -> list[str]:
    return [card_id for card_id, _decision, _ticker, _inner in _CARD_RE.findall(body)]


def _horizon(body: str, horizon_days: int) -> str:
    found = dict(_HORIZON_RE.findall(body))
    assert str(horizon_days) in found, f"no summary for the {horizon_days}-day horizon"
    return html.unescape(found[str(horizon_days)])


def _summary_text(body: str) -> str:
    """The summary's visible text, tags stripped, so a check reads what the owner reads."""
    joined = " ".join(_horizon(body, horizon) for horizon in _HORIZONS)
    return re.sub(r"<[^>]*>", " ", joined)


def _pas(client: TestClient, *, ticker: str = "SPY", direction: str = "up") -> str:
    response = press(client, "pas", ticker=ticker, direction=direction)
    assert response.status_code == 303
    return card_id_from(response.headers["location"], "pas")


def _log(client: TestClient, *, ticker: str = "NVDA", direction: str = "down") -> str:
    response = press(client, "log", ticker=ticker, direction=direction)
    assert response.status_code == 303
    return card_id_from(response.headers["location"], "card_id")


def _bulk(url: str, *, decision: str, n: int, measured: int, excess: float = 1.5) -> list[str]:
    """``n`` cards of one decision, ``measured`` of them with a computed outcome."""
    engine = make_engine(url)
    try:
        card_repo = cards.CardRepo(engine)
        outcome_repo = outcomes.OutcomeRepo(engine)
        written = [
            card_repo.write_card(
                decision=decision,  # type: ignore[arg-type]
                ticker="AAA",
                direction="yukarı",
                run_id="live-2026-09-16",
                dominant_option_symbol=None,
                card={},
                board_profile_hash="hash",
                calibration_profile_hash=None,
            )
            for _ in range(n)
        ]
        for i, card_id in enumerate(written[:measured]):
            outcome_repo.write_final(
                card_id=card_id,
                horizon_days=_HORIZONS[0],
                final=outcomes.FinalOutcome(
                    status=outcomes.COMPUTED,
                    measurement=outcomes.measure(
                        direction="yukarı",
                        underlying_at_card_day=100.0,
                        underlying_at_horizon=100.0 + excess + i,
                        spy_at_card_day=400.0,
                        spy_at_horizon=400.0,
                    ),
                ),
            )
        return written
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_a_pass_and_a_log_both_land_on_the_ledger_newest_first(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    first = _pas(client)
    second = _log(client)
    body = _defter(client)
    assert _order(body) == [second, first]  # newest first
    assert f'href="/kart/{first}"' in body
    assert f'href="/kart/{second}"' in body


def test_the_page_shows_the_cards_own_frozen_face(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    """Not a fresh read of today's board: every cell is the snapshot's own text."""
    client, _m, url = board
    card_id = _pas(client)
    engine = make_engine(url)
    try:
        stored = cards.CardRepo(engine).get_card(card_id)
    finally:
        engine.dispose()
    assert stored is not None
    face = card_face(stored)
    block = html.unescape(_blocks(_defter(client))[card_id])
    assert face.strength is not None and face.strength in block
    assert face.reason is not None and face.reason in block
    assert face.counter is not None and face.counter in block
    assert face.evidence_counts_text in block
    assert "SPY yukarı" in block
    assert "pas geçildi" in block


def test_a_card_whose_snapshot_cannot_be_read_still_renders(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    (card_id,) = _bulk(url, decision="pas", n=1, measured=0)
    block = html.unescape(_blocks(_defter(client))[card_id])
    assert "bilinmiyor" in block  # never a raised page, never an invented value


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_the_decision_filter_lists_only_that_decision(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    passed = _pas(client)
    logged = _log(client)
    assert _order(_defter(client, karar="pas")) == [passed]
    assert _order(_defter(client, karar="log")) == [logged]


def test_the_ticker_filter_matches_the_cards_stored_spelling(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    spy = _pas(client)
    nvda = _log(client)
    assert _order(_defter(client, hisse="nvda")) == [nvda]  # lower case, stored upper
    assert _order(_defter(client, hisse=" spy ")) == [spy]
    assert _order(_defter(client, hisse="MSFT")) == []


def test_an_unknown_decision_filter_lists_everything_rather_than_nothing(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    passed = _pas(client)
    logged = _log(client)
    for value in ("", "hepsi", "LOG", "sil"):
        assert set(_order(_defter(client, karar=value))) == {passed, logged}, value


def test_an_empty_filter_says_which_kind_of_nothing_it_is(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    assert "Kayıtlı karar kartı yok." in html.unescape(_defter(client))
    _pas(client)
    assert "Bu süzgeçle eşleşen kart yok." in html.unescape(_defter(client, hisse="MSFT"))


def test_the_filter_form_keeps_the_active_values(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    _pas(client)
    body = _defter(client, karar="pas", hisse="SPY")
    assert '<option value="pas" selected>' in body
    assert 'name="hisse" value="SPY"' in body
    # The board never carries the old dashboard's score controls again.
    for banned in ('name="ticker"', 'name="sort"', 'name="min_score"', 'name="label"'):
        assert banned not in body, banned


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_an_outcome_renders_on_its_own_card(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    card_id = _pas(client)
    engine = make_engine(url)
    try:
        measurement = outcomes.measure(
            direction="yukarı",
            underlying_at_card_day=100.0,
            underlying_at_horizon=102.0,
            spy_at_card_day=400.0,
            spy_at_horizon=402.0,
        )
        outcomes.OutcomeRepo(engine).write_final(
            card_id=card_id,
            horizon_days=_HORIZONS[0],
            final=outcomes.FinalOutcome(
                status=outcomes.COMPUTED, measurement=measurement, option_bid_at_horizon=0.75,
            ),
        )
    finally:
        engine.dispose()
    block = html.unescape(_blocks(_defter(client))[card_id])
    assert "hesaplandı" in block
    assert "+%1.5" in block  # +2.0% name - +0.5% SPY, in the card's direction
    assert "$0.75" in block


def test_a_horizon_with_no_row_is_never_a_zero(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    card_id = _pas(client)
    block = html.unescape(_blocks(_defter(client))[card_id])
    assert block.count("henüz hesaplanmadı") == len(_HORIZONS)
    assert "%0" not in block


def test_every_profile_horizon_has_a_summary(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    _pas(client)
    body = _defter(client)
    for horizon in _HORIZONS:
        assert f"{horizon} gün" in _horizon(body, horizon)


# ---------------------------------------------------------------------------
# The count gate (contract §4), rendered
# ---------------------------------------------------------------------------


def test_below_the_gate_the_summary_shows_counts_and_nothing_else(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _bulk(url, decision="pas", n=_MIN_N - 1, measured=_MIN_N - 1)
    summary = _horizon(_defter(client), _HORIZONS[0])
    assert f"{_MIN_N - 1} pas, 0 log; istatistik için yetersiz örnek" in summary
    assert "Medyan fark" not in summary
    assert "Çeyrekler arası aralık" not in summary


def test_at_the_gate_that_group_reports_a_median(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _bulk(url, decision="pas", n=_MIN_N, measured=_MIN_N)
    summary = _horizon(_defter(client), _HORIZONS[0])
    assert f"{_MIN_N} pas, 0 log" in summary
    assert "istatistik için yetersiz örnek" not in summary
    assert "Medyan fark" in summary
    assert f"{_MIN_N} kart" in summary
    # The other side has its own empty sample and stays gated.
    assert 'data-group="log"' not in summary
    assert 'data-group="pas"' in summary


def test_the_summary_never_claims_significance(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _bulk(url, decision="pas", n=_MIN_N, measured=_MIN_N)
    body = html.unescape(_defter(client))
    assert "Bu bir anlamlılık iddiası değildir." in body
    assert "Log ve pas kartları aynı hesapla ölçülür." in body
    # Read what the owner reads: the summary's own text, without the page chrome
    # (a raw substring check over the whole document matches charset="utf-8").
    summary = _summary_text(body)
    for banned in ("t=", "t-stat", "p-value", "isabet", "kazanma", "ortalama", "olasılık"):
        assert banned not in summary, banned


# ---------------------------------------------------------------------------
# The cap, failures, budget and honesty
# ---------------------------------------------------------------------------


def test_the_cap_is_disclosed_and_comes_from_the_profile(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, _url = board
    _pas(client)
    body = html.unescape(_defter(client))
    assert f"En son {_CAP} kart listelenir." in body
    assert _CAP == 200  # the profile's value, disclosed on the page
    assert "Liste doldu" not in body  # one card is not a full page


def test_a_full_page_says_older_cards_are_not_on_it(
    board: tuple[TestClient, ModuleType, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, m, url = board
    smaller = _SETTINGS.model_copy(
        update={"ledger": _SETTINGS.ledger.model_copy(update={"max_cards_per_page": 2})},
    )
    monkeypatch.setattr(m, "_board_settings", lambda: smaller)
    _bulk(url, decision="pas", n=3, measured=0)
    body = html.unescape(_defter(client))
    assert len(_order(body)) == 2
    assert "Liste doldu; daha eski kartlar bu sayfada yok." in body


def test_a_failed_read_says_nothing_was_deleted(
    board: tuple[TestClient, ModuleType, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, m, _url = board

    def _broken() -> cards.CardRepo:
        msg = "card table unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(m, "_card_repo", _broken)
    body = html.unescape(_defter(client))
    assert 'data-state="load-failed"' in body
    assert "hiçbir kayıt silinmedi" in body


def test_the_ledger_makes_zero_unusual_whales_calls(
    board: tuple[TestClient, ModuleType, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _m, url = board
    card_id = _pas(client)

    async def _no_uw(*args: Any, **kwargs: Any) -> dict[str, Any]:
        msg = "the pass ledger must not call Unusual Whales"
        raise AssertionError(msg)

    def _no_http(*args: Any, **kwargs: Any) -> httpx.Response:
        msg = "the pass ledger must not reach the network"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    monkeypatch.setattr(httpx, "get", _no_http)
    assert card_id in _blocks(_defter(client))
    assert card_id in _blocks(_defter(client, karar="pas", hisse="SPY"))
    _bulk(url, decision="log", n=1, measured=1)
    assert len(_order(_defter(client))) == 2


def test_no_forbidden_word_reaches_the_page(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _pas(client)
    _log(client)
    _bulk(url, decision="pas", n=_MIN_N, measured=_MIN_N)
    for params in ({}, {"karar": "pas"}, {"hisse": "SPY"}):
        body = html.unescape(_defter(client, **params))
        assert forbidden_words(body) == [], params


def test_the_auth_enumeration_covers_the_new_route() -> None:
    assert ("GET", "/defter") in _CASES
