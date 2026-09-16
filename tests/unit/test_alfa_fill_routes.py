"""Phase 5.2.C3: the ``Dolum gir`` form, ``POST /alfa/fill`` and the card page.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (append-only),
§5 (C3) and §6.

The board is seeded with two rows: SPY yukarı, whose dominant contract has a
live quote (bid 0.99 / ask 1.01, 41 s old at the page clock), and NVDA aşağı,
which has no quote at all. Pressing ``Logla`` on each gives a card with and a
card without an assumption to test.

Pins:
  - the assumed quote is read from the CARD SNAPSHOT on the server: forged form
    fields change nothing, and the stored fill carries the card's own bid, ask,
    mid and quote age;
  - slippage on hand-computed fills, entry and exit, rendered on the page;
  - the quote is labelled with its age at the card and never as the NBBO at the
    fill;
  - the POST is hardened exactly like the card POST (Origin/Referer must match
    the host, 403 otherwise, and nothing is written);
  - the count gate at ``fills.min_n_for_stats``, rendered, in both directions;
  - append-only: a second fill appends and the first is untouched;
  - a ``pas`` card and a card without a quote are refused, and no form is
    offered on either;
  - a journal trade that came from a card carries the form, and the fill it
    writes carries that trade's id — taken from the card, not from the client;
  - the fill routes make zero Unusual Whales calls;
  - every rendered Turkish string is clean, and the auth enumeration covers the
    new routes without editing it.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy.orm import Session
from webapp.board import cards, fills
from webapp.board.db import make_engine
from webapp.board.honesty import forbidden_words
from webapp.board.quotes import QuoteSnapshot, ensure_quotes_tables, upsert_quotes
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from tests.unit._alfa_card_harness import ORIGIN, card_id_from, reset_singletons, trade_form
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
_QUOTE_AGE = 41
_SYMBOL = "SPY260918C00760000"
# bid 0.99 / ask 1.01 → mid 1.00, so a five-cent miss is exactly $5.00 and exactly 5%.
_BID, _ASK = 0.99, 1.01
_ENTRY_FILL = "1.06"   # 1.06 − 1.01 ask  = +0.05 → +$5.00, +5%
_EXIT_FILL = "0.94"    # 0.99 bid − 0.94  = +0.05 → +$5.00, +5%

# (event_id, ticker, option_type, strike, premium, option_chain)
_SPECS = (
    ("s1", "SPY", "call", "760", "400000", _SYMBOL),
    ("n1", "NVDA", "put", "170", "80000", None),
)


def _seed(url: str) -> None:
    """One run with a quoted SPY row and an unquoted NVDA row."""
    store = SqliteBacktestStore(url, flush_threshold=len(_SPECS) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for event_id, ticker, option_type, strike, premium, _chain in _SPECS:
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=event_id, ts=_TS, ticker=ticker,
                        option_type=option_type, strike=strike, dte=2, premium=premium,  # type: ignore[arg-type]
                    ),
                    combined_score_post_penalty=0.42,
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        ensure_telemetry_tables(engine)
        ensure_quotes_tables(engine)
        with Session(engine) as session:
            session.add_all(
                AlfaPrintMeta(
                    run_id=_RUN, event_id=event_id, ticker=ticker, option_chain=chain,
                    fill_side="at_ask", option_type=option_type, strike=strike,
                    expiry=(_TS + timedelta(days=2)).date(), print_ts=_TS, written_at=_TS,
                )
                for event_id, ticker, option_type, strike, _premium, chain in _SPECS
            )
            session.commit()
        upsert_quotes(
            engine,
            [
                QuoteSnapshot(
                    option_symbol=_SYMBOL, ticker="SPY", nbbo_bid=_BID, nbbo_ask=_ASK,
                    last_price=_ASK, volume=500, open_interest=1000,
                    last_tape_time=None, returned=True,
                ),
            ],
            fetched_at=_NOW - timedelta(seconds=_QUOTE_AGE),
        )
    finally:
        engine.dispose()


def _reset(m: ModuleType) -> None:
    reset_singletons(m)
    m._FILLS = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'fills-route.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)  # no price fetch on the journal POST
    import webapp.main as m

    _reset(m)
    monkeypatch.setattr(m, "_now", lambda: _NOW)  # the quote age is clock-driven
    _seed(url)
    yield authed_client(m.app, monkeypatch), m
    _reset(m)


def _log_card(client: TestClient, *, ticker: str = "SPY", direction: str = "up") -> str:
    """Press ``Logla`` on one row and return the card id the redirect carries."""
    response = client.post(
        "/alfa/card",
        data={
            "decision": "log", "card_run_id": _RUN,
            "card_ticker": ticker, "card_direction": direction,
        },
        headers=ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 303
    return card_id_from(response.headers["location"], "card_id")


def _pas_card(client: TestClient) -> str:
    response = client.post(
        "/alfa/card",
        data={
            "decision": "pas", "card_run_id": _RUN,
            "card_ticker": "SPY", "card_direction": "up",
        },
        headers=ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 303
    return card_id_from(response.headers["location"], "pas")


def _fill(
    client: TestClient, card: str, *, side: str = "giriş", price: str = _ENTRY_FILL,
    contracts: str = "1", headers: dict[str, str] | None = None, **extra: str,
) -> httpx.Response:
    # ``card`` rather than ``card_id`` so a test can post a FORGED ``card_id`` field
    # through ``extra`` — the route must ignore any field it did not ask for.
    data = {
        "fill_card_id": card, "fill_side": side,
        "fill_price": price, "fill_contracts": contracts,
    }
    data.update(extra)
    return client.post(
        "/alfa/fill", data=data,
        headers=ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _visible(body: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


def _side_block(body: str, side: str) -> str:
    found = re.search(rf'data-side-summary="{side}".*?</div>', body, re.S)
    assert found is not None, side
    return found.group(0)


# ---------------------------------------------------------------------------
# The assumed quote comes from the card snapshot, on the server
# ---------------------------------------------------------------------------


def test_the_fill_carries_the_cards_own_quote_and_its_age(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert _fill(client, card_id).status_code == 303

    (stored,) = m._fill_repo().list_fills()
    assert stored.card_id == card_id
    assert stored.side == "giriş"
    assert stored.fill_price == 1.06
    assert stored.contracts == 1.0
    assert (stored.assumed_bid, stored.assumed_ask, stored.assumed_mid) == (_BID, _ASK, 1.0)
    assert stored.quote_age_seconds_at_card == _QUOTE_AGE

    # And it is the same quote the card froze, not a second read of the table.
    card = m._card_repo().get_card(card_id)
    assert card is not None
    assert fills.assumed_quote_from_card(card.card) == stored.quote


def test_client_sent_quote_values_are_ignored_entirely(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert _fill(
        client, card_id,
        assumed_bid="99.0", assumed_ask="0.01", assumed_mid="99.0",
        quote_age_seconds_at_card="0", trade_id="forged-trade", card_id="forged-card",
        slippage_usd="-999",
    ).status_code == 303

    (stored,) = m._fill_repo().list_fills()
    assert (stored.assumed_bid, stored.assumed_ask, stored.assumed_mid) == (_BID, _ASK, 1.0)
    assert stored.quote_age_seconds_at_card == _QUOTE_AGE
    assert stored.card_id == card_id
    assert stored.trade_id is None
    assert stored.slippage_usd == 5.0


# ---------------------------------------------------------------------------
# Slippage, both sides, on the page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "price", "usd", "pct"),
    [
        ("giriş", _ENTRY_FILL, "+$5.00", "+%5"),
        ("giriş", "1.01", "+$0.00", "+%0"),
        ("giriş", "0.96", "-$5.00", "-%5"),
        ("çıkış", _EXIT_FILL, "+$5.00", "+%5"),
        ("çıkış", "0.99", "+$0.00", "+%0"),
        ("çıkış", "1.04", "-$5.00", "-%5"),
    ],
    ids=["entry worse", "entry at ask", "entry better",
         "exit worse", "exit at bid", "exit better"],
)
def test_slippage_is_rendered_for_both_sides(
    board: tuple[TestClient, ModuleType], side: str, price: str, usd: str, pct: str,
) -> None:
    client, _m = board
    card_id = _log_card(client)
    assert _fill(client, card_id, side=side, price=price).status_code == 303

    body = client.get(f"/kart/{card_id}").text
    assert f"data-slippage-usd>{usd}<" in body
    assert f"data-slippage-pct>{pct}<" in body
    assert f'data-fill-side-value="{side}"' in body


def test_the_quote_is_labelled_with_its_age_at_the_card_never_as_the_nbbo_at_the_fill(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, _m = board
    card_id = _log_card(client)
    assert _fill(client, card_id).status_code == 303

    body = client.get(f"/kart/{card_id}").text
    text = _visible(body)
    assert f"kart anındaki kotasyon, {_QUOTE_AGE} sn yaşında" in text
    assert "bid $0.99 / ask $1.01" in text
    assert fills.FILL_COPY["assumed_quote_not_nbbo"] in text
    # The only NBBO on the page is the one that says it does not exist.
    assert text.count("NBBO") == 1
    for claim in ("NBBO at fill", "dolum anındaki kotasyon", "canlı"):
        assert claim not in text, claim


def test_the_confirmation_names_the_fill_that_was_written(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    location = _fill(client, card_id, contracts="3").headers["location"]
    assert location.startswith(f"/kart/{card_id}?dolum=")

    (stored,) = m._fill_repo().list_fills()
    body = client.get(location).text
    assert 'data-state="fill-recorded"' in body
    assert fills.recorded_text(stored, ticker="SPY") in _visible(body)

    # A made-up ?dolum= claims nothing, and neither does another card's fill.
    forged = client.get(f"/kart/{card_id}", params={"dolum": "no-such-fill"}).text
    assert 'data-state="fill-recorded"' not in forged


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
def test_a_fill_post_that_did_not_come_from_this_host_is_refused(
    board: tuple[TestClient, ModuleType], headers: dict[str, str],
) -> None:
    client, m = board
    card_id = _log_card(client)
    response = _fill(client, card_id, headers=headers)
    assert response.status_code == 403
    assert response.text == cards.CARD_COPY["forbidden_origin"]
    assert m._fill_repo().list_fills() == ()


def test_a_referer_from_this_host_is_accepted(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    card_id = _log_card(client)
    response = _fill(client, card_id, headers={"Referer": f"http://testserver/kart/{card_id}"})
    assert response.status_code == 303
    assert len(m._fill_repo().list_fills()) == 1


# ---------------------------------------------------------------------------
# What is refused, and what is appended
# ---------------------------------------------------------------------------


def test_a_pas_card_takes_no_fill(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    card_id = _pas_card(client)
    response = _fill(client, card_id)
    assert response.status_code == 400
    assert response.text == fills.FILL_COPY["not_logged"]
    assert m._fill_repo().list_fills() == ()

    body = client.get(f"/kart/{card_id}").text
    assert 'data-state="not-logged"' in body
    assert 'action="/alfa/fill"' not in body  # no form is offered at all


def test_a_card_without_a_quote_takes_no_fill(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    card_id = _log_card(client, ticker="NVDA", direction="down")
    response = _fill(client, card_id)
    assert response.status_code == 400
    assert response.text == fills.FILL_COPY["no_assumed_quote"]
    assert m._fill_repo().list_fills() == ()

    body = client.get(f"/kart/{card_id}").text
    assert 'data-state="no-assumed-quote"' in body
    assert 'action="/alfa/fill"' not in body
    assert fills.FILL_COPY["no_assumed_quote"] in _visible(body)


@pytest.mark.parametrize(
    ("fields", "status", "copy_key"),
    [
        ({"side": "entry"}, 400, "unknown_side"),
        ({"side": "GİRİŞ"}, 400, "unknown_side"),
        ({"price": "0"}, 400, "bad_numbers"),
        ({"price": "-1.5"}, 400, "bad_numbers"),
        ({"contracts": "0"}, 400, "bad_numbers"),
        ({"contracts": "-2"}, 400, "bad_numbers"),
        # A blank side or price never reaches the handler: the form fields are
        # required, so FastAPI refuses the request. Nothing is written either way,
        # which is the point.
        ({"side": ""}, 422, None),
        ({"price": ""}, 422, None),
    ],
    ids=["unknown side", "wrong case", "zero price", "negative price",
         "zero contracts", "negative contracts", "blank side", "blank price"],
)
def test_a_fill_that_cannot_be_measured_writes_nothing(
    board: tuple[TestClient, ModuleType], fields: dict[str, str], status: int,
    copy_key: str | None,
) -> None:
    client, m = board
    card_id = _log_card(client)
    response = _fill(client, card_id, **fields)  # type: ignore[arg-type]
    assert response.status_code == status
    if copy_key is not None:
        assert response.text == fills.FILL_COPY[copy_key]
    assert m._fill_repo().list_fills() == ()


def test_an_unknown_card_is_refused(board: tuple[TestClient, ModuleType]) -> None:
    client, m = board
    response = _fill(client, "no-such-card")
    assert response.status_code == 404
    assert response.text == fills.FILL_COPY["card_not_found"]
    assert m._fill_repo().list_fills() == ()
    assert client.get("/kart/no-such-card").status_code == 404
    assert 'data-state="card-missing"' in client.get("/kart/no-such-card").text


def test_a_second_fill_appends_and_leaves_the_first_alone(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert _fill(client, card_id, side="giriş", price=_ENTRY_FILL).status_code == 303
    (first,) = m._fill_repo().list_fills()

    assert _fill(client, card_id, side="çıkış", price=_EXIT_FILL).status_code == 303
    written = m._fill_repo().list_fills()
    assert len(written) == 2
    assert len({f.id for f in written}) == 2
    unchanged = m._fill_repo().get_fill(first.id)
    assert unchanged == first  # append-only: the first record is untouched


def test_the_app_exposes_no_route_that_could_edit_or_remove_a_fill() -> None:
    dangerous = {"PUT", "PATCH", "DELETE"}
    for method, path in _CASES:
        assert method not in dangerous, (method, path)


# ---------------------------------------------------------------------------
# The count gate, rendered, at the boundary in both directions (contract §5)
# ---------------------------------------------------------------------------


def _write_fills(m: ModuleType, card_id: str, count: int, *, side: str = "giriş") -> None:
    """Record ``count`` fills straight through the repo (the POST is tested above)."""
    card = m._card_repo().get_card(card_id)
    assert card is not None
    quote = fills.assumed_quote_from_card(card.card)
    assert quote is not None
    repo = m._fill_repo()
    for i in range(count):
        repo.write_fill(
            card_id=card_id, trade_id=None, side=side,
            fill_price=round(1.02 + i / 100, 2), contracts=1.0, quote=quote,
        )


def test_below_the_gate_the_summary_shows_a_count_and_no_statistic(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    min_n = m._board_settings().fills.min_n_for_stats
    card_id = _log_card(client)
    _write_fills(m, card_id, min_n - 1)

    block = _side_block(client.get(f"/kart/{card_id}").text, "giriş")
    assert "data-counts-only" in block
    assert f"{min_n - 1} dolum kaydı; istatistik için yetersiz örnek" in html.unescape(block)
    for statistic in ("data-median-usd", "data-median-pct", "data-iqr-usd", "data-sample-size"):
        assert statistic not in block, statistic


def test_at_the_gate_the_median_and_interquartile_range_appear(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    min_n = m._board_settings().fills.min_n_for_stats
    card_id = _log_card(client)
    _write_fills(m, card_id, min_n)

    body = client.get(f"/kart/{card_id}").text
    block = _side_block(body, "giriş")
    assert "data-counts-only" not in block
    for statistic in ("data-median-usd", "data-median-pct", "data-iqr-usd"):
        assert statistic in block, statistic
    assert f"{min_n} dolum kaydı" in html.unescape(block)

    # The other side is gated on its own sample, and has none.
    empty = _side_block(body, "çıkış")
    assert "data-counts-only" in empty
    assert "0 dolum kaydı; istatistik için yetersiz örnek" in html.unescape(empty)

    # And no sample size ever buys a significance claim. The one sentence that may
    # say "anlamlılık" is the disclaimer itself, so it is removed before the scan.
    text = _visible(body)
    assert fills.FILL_COPY["no_significance"] in text
    scanned = text.replace(fills.FILL_COPY["no_significance"], " ")
    for claim in ("t=", "t-stat", "anlamlı", "kazanma oranı", "ortalama"):
        assert claim not in scanned, claim


def test_the_gate_reads_the_profile_and_is_not_a_literal_in_the_page(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert m._board_settings().fills.min_n_for_stats == 20  # profiles/board_v1.yaml
    card_id = _log_card(client)
    _write_fills(m, card_id, 1)
    block = _side_block(client.get(f"/kart/{card_id}").text, "giriş")
    assert "1 dolum kaydı; istatistik için yetersiz örnek" in html.unescape(block)


# ---------------------------------------------------------------------------
# The journal side: a trade that came from a card
# ---------------------------------------------------------------------------


def test_a_journal_trade_from_a_card_carries_the_form_and_the_cards_quote(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert client.post(
        "/journal", data=trade_form(card_id=card_id), follow_redirects=False,
    ).status_code == 303
    (trade,) = m._journal().list()

    body = client.get("/journal").text
    assert 'action="/alfa/fill"' in body
    assert f'value="{card_id}"' in body
    assert f'data-card-link="{card_id}"' in body
    assert f"kart anındaki kotasyon, {_QUOTE_AGE} sn yaşında" in _visible(body)
    # The journal shows one trade's own records; the sample-wide summary lives on
    # the card page, so it is not repeated once per trade.
    assert "data-slippage-summary" not in body

    # The fill takes its trade id from the card, not from the form.
    assert _fill(client, card_id, trade_id="forged-trade").status_code == 303
    (stored,) = m._fill_repo().list_fills()
    assert stored.trade_id == trade.id
    assert f"data-fill=\"{stored.id}\"" in client.get("/journal").text


def test_a_journal_trade_without_a_card_is_untouched(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert client.post("/journal", data=trade_form(), follow_redirects=False).status_code == 303
    assert len(m._journal().list()) == 1
    body = client.get("/journal").text
    assert body.count('action="/alfa/fill"') == 0  # D10: the old journal flow is unchanged
    assert "data-card-link" not in body
    assert client.get("/journal").status_code == 200


# ---------------------------------------------------------------------------
# Honesty, UW silence and the shared gate
# ---------------------------------------------------------------------------


def test_every_rendered_string_on_the_fill_surfaces_is_clean(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, _m = board
    for text in fills.FILL_COPY.values():
        assert forbidden_words(text) == [], text
    card_id = _log_card(client)
    assert client.post(
        "/journal", data=trade_form(card_id=card_id), follow_redirects=False,
    ).status_code == 303
    assert _fill(client, card_id).status_code == 303
    for path in (f"/kart/{card_id}", "/journal", "/kart/no-such-card"):
        assert forbidden_words(_visible(client.get(path).text)) == [], path


def test_the_fill_routes_make_zero_unusual_whales_calls(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _m = board
    card_id = _log_card(client)
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"the fill routes must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    def _no_http(url: str, **_kwargs: Any) -> httpx.Response:
        calls.append(url)
        msg = f"the fill routes must not reach the network ({url})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    monkeypatch.setattr(httpx, "get", _no_http)

    assert _fill(client, card_id).status_code == 303
    for path in (f"/kart/{card_id}", "/journal"):
        assert client.get(path).status_code == 200
    assert calls == []


def test_the_auth_enumeration_covers_the_fill_routes() -> None:
    """The route-enumerating gate test picks the new routes up without editing it."""
    assert ("POST", "/alfa/fill") in _CASES
    assert ("GET", "/kart/dummy") in _CASES
