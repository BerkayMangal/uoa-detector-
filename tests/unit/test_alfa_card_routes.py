"""Phase 5.2.C1b: the ``Logla`` / ``Pas geç`` buttons and ``POST /alfa/card``.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §3 (C1) and §6.

Pins:
  - the frozen snapshot equals the row view the server rebuilds at that moment,
    and forged form fields change nothing about it;
  - a request whose ``Origin`` or ``Referer`` is not this host is 403 and writes
    nothing; so is one carrying neither header;
  - one press writes exactly one card, and a second press appends rather than
    rewriting (append-only);
  - ``Pas geç`` comes back to the board with a confirmation that names a card
    which really exists; a made-up ``?pas=`` claims nothing;
  - ``Logla`` goes to the journal form with the card id, and saving the trade
    links it onto the card once;
  - the card routes make zero Unusual Whales calls;
  - every generated Turkish string passes ``honesty.ensure_clean``, and the
    rendered board carries no forbidden word;
  - the route-enumerating auth test covers the new POST automatically.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from webapp.board import cards
from webapp.board.evidence import request_for
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.penalty_ledger import read_profile_hashes
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from tests.unit.test_webapp_auth import _CASES
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient

_RUN = "live-2026-09-16"
_TS = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 16, 14, 5, tzinfo=UTC)
_ORIGIN = {"Origin": "http://testserver"}
_ROW_RE = re.compile(r"<article[^>]*data-row.*?</article>", re.S)

# Two rows: SPY yukarı (two bought calls on one strike) and NVDA aşağı (a bought put).
_SPECS = (
    ("s1", "SPY", "call", "760", "400000", 0),
    ("s2", "SPY", "call", "760", "100000", 10),
    ("n1", "NVDA", "put", "170", "80000", 20),
)


def _seed(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(_SPECS) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for event_id, ticker, option_type, strike, premium, second in _SPECS:
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=event_id, ts=_TS + timedelta(seconds=second), ticker=ticker,
                        option_type=option_type, strike=strike, dte=2, premium=premium,  # type: ignore[arg-type]
                    ),
                    combined_score_post_penalty=0.42,
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = create_engine(url)
    try:
        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            session.add_all(
                AlfaPrintMeta(
                    run_id=_RUN, event_id=event_id, ticker=ticker, option_chain=None,
                    fill_side="at_ask", option_type=option_type, strike=strike,
                    expiry=(_TS + timedelta(days=2)).date(), print_ts=_TS, written_at=_TS,
                )
                for event_id, ticker, option_type, strike, _premium, _second in _SPECS
            )
            session.commit()
    finally:
        engine.dispose()


def _reset(m: ModuleType) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None
    m._CARDS = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'cards-route.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)  # no price fetch on the journal POST
    import webapp.main as m

    _reset(m)
    monkeypatch.setattr(m, "_now", lambda: _NOW)  # quote and tape ages are clock-driven
    _seed(url)
    yield authed_client(m.app, monkeypatch), m
    _reset(m)


def _press(
    client: TestClient, decision: str = "pas", *, ticker: str = "SPY", direction: str = "up",
    run_id: str = _RUN, headers: dict[str, str] | None = None, **extra: str,
) -> httpx.Response:
    # The form's identity fields are namespaced (see the route): the board carries no
    # control named "ticker", "label", "sort" or "min_score" any more.
    data = {
        "decision": decision, "card_run_id": run_id,
        "card_ticker": ticker, "card_direction": direction,
    }
    data.update(extra)
    return client.post(
        "/alfa/card", data=data,
        headers=_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _visible(body: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


def _rebuilt_view(m: ModuleType, ticker: str, direction: str) -> Any:
    prints = m._board_reader().load_run(_RUN)
    page = m._build_board_page(prints, gate_on=True, run_latest_ts=None)
    return next(v for v in page.views if v.row.ticker == ticker and v.row.direction == direction)


# ---------------------------------------------------------------------------
# The snapshot the server freezes
# ---------------------------------------------------------------------------


def test_the_card_freezes_the_row_view_the_server_rebuilds(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert _press(client, "pas").status_code == 303

    (card,) = m._card_repo().list_cards()
    view = _rebuilt_view(m, "SPY", "up")
    frozen = card.card

    assert frozen["row_view"] == cards.snapshot(view)
    assert sorted(frozen["constituents"]) == [[_RUN, "s1"], [_RUN, "s2"]]

    board_hash = m._board_settings().content_hash()
    event_id = request_for(view.row).event_id
    calibration = read_profile_hashes(m._board_reader().engine, _RUN, [event_id]).get(event_id)
    assert calibration is not None  # the run's signals record the profile that scored them
    assert frozen["board_profile_hash"] == board_hash
    assert frozen["calibration_profile_hash"] == calibration

    assert card.decision == "pas"
    assert card.ticker == "SPY"
    assert card.direction == "yukarı"  # contract §2 stores the board's own label
    assert card.run_id == _RUN
    assert card.dominant_option_symbol == view.symbol
    assert card.board_profile_hash == board_hash
    assert card.calibration_profile_hash == calibration
    assert card.trade_id is None
    assert card.note is None


def test_client_sent_values_are_ignored_entirely(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    assert _press(
        client, "pas",
        card_json='{"ticker": "EVIL"}',
        board_profile_hash="forged-hash",
        calibration_profile_hash="forged-hash",
        dominant_option_symbol="EVIL260918C00000000",
        total_premium="999999999",
        clean_candidate="true",
        note="forged note",
        trade_id="forged-trade",
        board_version="forged-version",
    ).status_code == 303

    (card,) = m._card_repo().list_cards()
    view = _rebuilt_view(m, "SPY", "up")
    assert "EVIL" not in card.card_json
    assert "forged" not in card.card_json
    assert card.board_profile_hash == m._board_settings().content_hash()
    assert card.calibration_profile_hash != "forged-hash"
    assert card.dominant_option_symbol == view.symbol
    assert card.board_version == cards.UNKNOWN_BOARD_VERSION
    assert card.note is None
    assert card.trade_id is None
    assert card.card["row_view"] == cards.snapshot(view)


# ---------------------------------------------------------------------------
# POST hardening (contract §3: Origin/Referer must match the request host)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.test"},
        {"Referer": "https://evil.test/board"},
        {"Origin": "https://evil.test", "Referer": "http://testserver/"},
        {},
    ],
    ids=["bad origin", "bad referer", "bad origin with a good referer", "neither header"],
)
def test_a_post_that_did_not_come_from_this_host_is_refused(
    board: tuple[TestClient, ModuleType], headers: dict[str, str],
) -> None:
    client, m = board
    response = _press(client, "pas", headers=headers)
    assert response.status_code == 403
    assert response.text == cards.CARD_COPY["forbidden_origin"]
    assert m._card_repo().list_cards() == ()


def test_a_referer_from_this_host_is_accepted(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    response = _press(client, "pas", headers={"Referer": "http://testserver/?run=" + _RUN})
    assert response.status_code == 303
    assert len(m._card_repo().list_cards()) == 1


# ---------------------------------------------------------------------------
# What is written, and what is refused
# ---------------------------------------------------------------------------


def test_one_press_writes_exactly_one_card_and_a_second_appends(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert _press(client, "pas").status_code == 303
    assert len(m._card_repo().list_cards()) == 1

    assert _press(client, "pas").status_code == 303
    written = m._card_repo().list_cards()
    assert len(written) == 2  # append-only: the second press is a second record
    assert len({c.id for c in written}) == 2


@pytest.mark.parametrize(
    ("fields", "status", "copy_key"),
    [
        ({"decision": "belki"}, 400, "unknown_decision"),
        ({"ticker": "NOSUCH"}, 404, "row_not_found"),
        ({"direction": "down"}, 404, "row_not_found"),  # SPY has no aşağı row in this run
        ({"run_id": "no-such-run"}, 404, "row_not_found"),
        # A blank run id never reaches the handler: the form field is required, so
        # FastAPI refuses the request. Nothing is written either way, which is the point.
        ({"run_id": ""}, 422, None),
    ],
    ids=["unknown decision", "unknown ticker", "no such direction", "unknown run", "blank run"],
)
def test_a_row_that_cannot_be_rebuilt_writes_nothing(
    board: tuple[TestClient, ModuleType], fields: dict[str, str], status: int,
    copy_key: str | None,
) -> None:
    client, m = board
    response = _press(client, **fields)  # type: ignore[arg-type]
    assert response.status_code == status
    if copy_key is not None:
        assert response.text == cards.CARD_COPY[copy_key]
    assert m._card_repo().list_cards() == ()


# ---------------------------------------------------------------------------
# Where the two buttons take the owner
# ---------------------------------------------------------------------------


def test_pas_returns_to_the_board_with_a_confirmation_naming_the_card(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    response = _press(client, "pas", gate="off")
    assert response.status_code == 303

    location = response.headers["location"]
    parts = urlsplit(location)
    params = dict(parse_qsl(parts.query))
    assert parts.path == "/"
    assert params["run"] == _RUN
    assert params["gate"] == "off"  # the owner's gate state survives the round trip

    card = m._card_repo().get_card(params["pas"])
    assert card is not None
    body = client.get(location).text
    assert 'data-state="card-recorded"' in body
    assert f'data-card-id="{card.id}"' in body
    assert cards.pas_recorded_text(card) in _visible(body)

    # A made-up card id claims nothing was written.
    forged = client.get("/", params={"pas": "no-such-card"}).text
    assert 'data-state="card-recorded"' not in forged


def test_log_goes_to_the_journal_form_carrying_the_card_id(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    response = _press(client, "log", ticker="NVDA", direction="down")
    assert response.status_code == 303

    location = response.headers["location"]
    parts = urlsplit(location)
    card_id = dict(parse_qsl(parts.query))["card_id"]
    assert parts.path == "/journal/new"

    card = m._card_repo().get_card(card_id)
    assert card is not None
    assert card.decision == "log"
    assert card.ticker == "NVDA"
    assert card.direction == "aşağı"

    form = client.get(location).text
    assert f'name="card_id" value="{card_id}"' in form
    assert card_id in _visible(form)


def test_saving_the_journal_trade_links_the_card_once(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = dict(parse_qsl(urlsplit(_press(client, "log").headers["location"]).query))["card_id"]
    trade = {
        "ticker": "SPY", "direction": "bullish", "instrument": "call",
        "contracts": "1", "entry_price": "2.50", "thesis": "board card",
    }

    assert client.post("/journal", data={**trade, "card_id": card_id},
                       follow_redirects=False).status_code == 303
    trades = m._journal().list()
    assert len(trades) == 1
    linked = m._card_repo().get_card(card_id)
    assert linked is not None
    assert linked.trade_id == trades[0].id

    # A second trade quoting the same card never re-links it, and never rewrites it.
    assert client.post("/journal", data={**trade, "card_id": card_id},
                       follow_redirects=False).status_code == 303
    again = m._card_repo().get_card(card_id)
    assert again is not None
    assert again.trade_id == linked.trade_id
    assert again.card_json == linked.card_json

    # And the journal still works with no card at all (D10: the old flow is unchanged).
    assert client.post("/journal", data=trade, follow_redirects=False).status_code == 303
    assert len(m._journal().list()) == 3


def test_an_unknown_card_id_never_costs_the_trade(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    assert client.post("/journal", data={
        "ticker": "SPY", "direction": "bullish", "instrument": "call",
        "contracts": "1", "entry_price": "2.50", "thesis": "manual", "card_id": "no-such-card",
    }, follow_redirects=False).status_code == 303
    assert len(m._journal().list()) == 1
    assert m._card_repo().list_cards() == ()


# ---------------------------------------------------------------------------
# Render, honesty and the shared gate
# ---------------------------------------------------------------------------


def test_every_row_carries_both_buttons_outside_the_audit_block(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, _m = board
    body = client.get("/", params={"gate": "off"}).text
    rows = _ROW_RE.findall(body)
    assert len(rows) == 2
    for row in rows:
        assert 'action="/alfa/card"' in row
        assert 'data-card-decision="log"' in row
        assert 'data-card-decision="pas"' in row
        assert 'name="card_ticker"' in row
        # The old dashboard's score filters stay gone (test_board_honesty pins this
        # for the whole page; here it is pinned at the source, the card form).
        for old_control in ('name="ticker"', 'name="label"', 'name="sort"', 'name="min_score"'):
            assert old_control not in row, old_control
        assert cards.CARD_COPY["log_button"] in row
        assert cards.CARD_COPY["pas_button"] in row
        # The buttons are a sibling of the audit block, never inside it, and add
        # no <details> of their own (template invariant, R-EV2).
        assert row.index("data-card-form") > row.rindex("</details>")
        assert row.count("<details") == row.count("</details>")
        assert "Birleşik skor" not in row.split("data-card-form")[1]


def test_generated_card_copy_is_clean(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    for text in cards.CARD_COPY.values():
        assert ensure_clean(text) == text

    location = _press(client, "pas").headers["location"]
    card = m._card_repo().get_card(dict(parse_qsl(urlsplit(location).query))["pas"])
    assert card is not None
    assert ensure_clean(cards.pas_recorded_text(card)) == cards.pas_recorded_text(card)

    # The whole rendered board, buttons and confirmation included.
    assert forbidden_words(_visible(client.get(location).text)) == []


def test_the_card_routes_make_zero_unusual_whales_calls(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _m = board
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"the card routes must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    def _no_http(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        msg = f"the card routes must not reach the network ({url})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    monkeypatch.setattr(httpx, "get", _no_http)

    assert client.get("/").status_code == 200
    for decision in ("pas", "log"):
        assert _press(client, decision).status_code == 303
    assert calls == []


def test_the_auth_enumeration_covers_the_card_route() -> None:
    """The route-enumerating gate test picks the new POST up without editing it."""
    assert ("POST", "/alfa/card") in _CASES
