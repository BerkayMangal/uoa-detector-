"""Phase 5.2.D2 (R-DL1): the İçeriden family renders inside the delayed bucket.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-DL1 and R-UN1, §7 D2.

Hermetic. Insider fixture rows keep the shape of the live
``/api/insider/transactions`` response captured 2026-09-15 (alfa_probe): the
amount is an integer, the price a string, ``ids`` the merge group. The seeded
run, the page clock and the row seeding come from the D1 render test.

Pins:
  - Kongre and İçeriden both render, in ``RENDERED_FAMILIES`` order;
  - an insider item carries its filing date, the delay, the filer and title,
    the side, the size line, the 10b5-1 and Form 4/A flags and the outcome;
  - adding the family changes nothing outside the bucket: counts, strength,
    clean-candidate flags, the R-EM1 banner, the AMA choice and the row order
    are identical with and without the delayed rows;
  - the insider size line is generated copy and passes ``ensure_clean``; the
    filer name is vendor data and never passes a raising guard.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
from webapp.board.db import make_engine, session_factory
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    INSIDER_TRANSACTIONS_PATH,
    run_delayed_job,
)
from webapp.board.delayed_panel import RENDERED_FAMILIES, load_delayed_panels
from webapp.board.honesty import ensure_clean, forbidden_words

from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_delayed_render import (
    _NOW,
    _ROWS,
    _SETTINGS,
    _TODAY,
    _reset,
    _rows_of,
    _seed_run,
    _text,
    _without_delayed,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine


def _insider(**fields: object) -> dict[str, Any]:
    return {
        "ticker": "AAA", "transactions": 1, "security_title": "Common Stock",
        "stock_price": "211.96", "is_officer": False, "is_director": True, **fields,
    }


_STEVENS = _insider(
    id="d30ff7d8-cded-4f1d-86f4-5428a1138c93", amount=-622239, transactions=4, price="231.6191",
    transaction_date="2026-09-04", filing_date="2026-09-08", formtype="4", transaction_code="S",
    is_10b5_1=False, owner_name="STEVENS MARK", officer_title="",
    ids=["d765fd55-0895-4893-b553-ebd5737fd470", "50c19559-5cc9-490a-af8a-902194ebc8ea"],
)
_TETER_10B5 = _insider(
    id="5193595c-3f98-4be8-9a59-0c73afd8e8d9", amount=-30000, transactions=3, price="217.8840",
    transaction_date="2026-08-31", filing_date="2026-09-02", formtype="4", transaction_code="S",
    is_10b5_1=True, owner_name="TETER TIMOTHY", officer_title="EVP, General Counsel and Sec",
    is_officer=True, is_director=False,
    ids=["6d1453b2-c28d-4ebe-b382-10600f28affd"],
)
_AMENDED_BUY = _insider(
    id="9f1b2c31-0d44-4f0a-9f21-2b6a9b3c77aa", amount=12000, price="205.5000",
    transaction_date="2026-08-20", filing_date="2026-08-24", formtype="4/A", transaction_code="P",
    is_10b5_1=False, owner_name="COXE TENCH", officer_title=None,
    ids=["9350b657-dd3c-42b8-ae51-adc4acdce4e6"],
)
# Not a trade: a grant. It must never reach the bucket (contract §7 D2).
_GRANT = _insider(
    id="3ab32345-c9bc-481f-91a1-d5657ff600a1", amount=172507, price="0.0000",
    transaction_date="2026-09-09", filing_date="2026-09-11", formtype="4", transaction_code="A",
    is_10b5_1=False, owner_name="PARKER NICHOLAS", officer_title="EVP, Worldwide Field Ops",
    ids=["82682939-3152-46c3-8f8b-e91a4dad6ceb"],
)

_CONGRESS_AAA = {
    "name": "Gilbert Cisneros", "ticker": "AAA", "issuer": "undisclosed", "txn_type": "Sell",
    "politician_id": "739eca36-a8f3-4894-96b1-420354fe17b6", "amounts": "$1,001 - $15,000",
    "transaction_date": "2026-08-18", "filed_at_date": "2026-09-11", "member_type": "house",
    "reporter": "Hon. Gilbert Cisneros",
}


class _FakeClient:
    """Congress and insider rows for AAA; every other family and ticker answers empty."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        args = params or {}
        if path == CONGRESS_RECENT_TRADES_PATH and args.get("ticker") == "AAA":
            return {"data": [_CONGRESS_AAA]}
        if path == INSIDER_TRANSACTIONS_PATH and args.get("ticker_symbol") == "AAA":
            return {"data": [_STEVENS, _TETER_10B5, _AMENDED_BUY, _GRANT], "has_more": False}
        return {"data": []}


def _seed_delayed(engine: Engine) -> None:
    asyncio.run(
        run_delayed_job(_FakeClient(), engine, ["AAA", "BBB"], now=_NOW, settings=_SETTINGS.delayed),  # type: ignore[arg-type]
    )
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as session:
        session.add_all([
            AlfaDailyClose(ticker="AAA", day=day, close=close, fetched_at=_NOW)
            for day, close in ((date(2026, 9, 8), 100.0), (date(2026, 9, 14), 103.0))
        ])
        session.commit()


@pytest.fixture
def board(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, TestClient]]:
    import webapp.board.delayed_panel as panel_module
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'insider.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)  # date-seeded: pin the page clock
    panel_module._tables_ready_for.clear()
    _reset(m)
    _seed_run(url)
    engine = make_engine(url)
    try:
        yield engine, authed_client(m.app, monkeypatch)
    finally:
        engine.dispose()
        panel_module._tables_ready_for.clear()
        _reset(m)


def test_both_shipped_families_render_in_order(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    _seed_delayed(engine)
    row = _rows_of(client.get("/alfa").text)["AAA"]
    assert RENDERED_FAMILIES[:2] == ("congress", "insider")  # later commits append families
    assert re.findall(r'data-delayed-family="(\w+)"', row)[:2] == ["congress", "insider"]
    assert "İçeriden" in _text(row)


def test_an_insider_item_carries_its_filing_date_filer_side_size_flags_and_outcome(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    row = _rows_of(client.get("/alfa").text)["AAA"]
    insider = row[row.index('data-delayed-family="insider"'):]
    text = _text(insider)

    assert "bildirim tarihi 2026-09-08" in text
    assert "işlemden 4 gün sonra bildirildi" in text
    assert "STEVENS MARK" in text
    assert "satış" in text
    assert "622,239 hisse x $231.62 ≈ $144,122,437" in text
    assert "bildirim tarihinden beri dayanak %+3.0 (2026-09-08 → 2026-09-14 kapanış)" in text
    # The scheduled plan, the amendment and the buy side each render their own label.
    assert "TETER TIMOTHY (EVP, General Counsel and Sec)" in text
    assert "10b5-1 planlı işlem" in text
    assert "düzeltilmiş beyan (Form 4/A)" in text
    assert "alış" in text
    # A grant is not a trade: it never reaches the bucket.
    assert "PARKER NICHOLAS" not in text
    assert insider.count("data-delayed-item=") == 3


def test_adding_the_family_changes_nothing_outside_the_bucket(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    before = client.get("/alfa").text
    _seed_delayed(engine)
    after = client.get("/alfa").text

    assert "data-delayed-item=" not in before
    assert _without_delayed(before) == _without_delayed(after)
    words = {t: re.findall(r"data-evidence-word>([^<]*)<", r) for t, r in _rows_of(after).items()}
    assert [html.unescape(w) for w in words["AAA"]] == ["1 lehte · 0 aleyhte · 5 bilinmiyor"]
    assert re.findall(r'data-clean-candidate="(\w+)"', after) == ["false", "false"]


def test_an_unfetched_insider_family_reads_bilinmiyor(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    body = client.get("/alfa").text
    assert body.count('data-delayed-family="insider"') == len(_ROWS)
    assert body.count('data-delayed-state="never_fetched"') == len(_ROWS) * len(RENDERED_FAMILIES)

    _seed_delayed(engine)
    rows = _rows_of(client.get("/alfa").text)
    bbb = rows["BBB"][rows["BBB"].index('data-delayed-family="insider"'):]
    assert 'data-delayed-state="empty"' in bbb  # the source answered with nothing
    assert "kayıt yok — kaynak yanıt verdi" in _text(bbb)


def test_the_insider_size_line_is_generated_copy_and_the_filer_name_is_vendor_data(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    assert client.get("/alfa").status_code == 200

    panels = load_delayed_panels(engine, ["AAA"], today=_TODAY, settings=_SETTINGS.delayed)
    insider = next(f for f in panels["AAA"].families if f.family == "insider")
    assert insider.items
    generated = [insider.label, *insider.notes]
    for item in insider.items:
        # Unlike a Kongre filed range, the insider size line is generated from the row.
        generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text]
        generated += [t for t in (item.size_text, item.side_text) if t is not None]
        generated += list(item.flags)
    for text in generated:
        assert ensure_clean(text) == text
    # The filer name is vendor data: it is shown, but never passed through a raising guard.
    assert [i.who for i in insider.items if i.who] != []
    assert forbidden_words("STEVENS MARK") == []
