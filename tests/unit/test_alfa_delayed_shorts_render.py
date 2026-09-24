"""Phase 5.2.D3 (R-DL1): the Short and FTD families render in the delayed bucket.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-DL1 and R-UN1, §7 D3.

Hermetic. Fixture rows keep the shape of the live responses captured
2026-09-15 (alfa_probe): ``interest-float/v2`` carries ``si_float`` as a string
fraction with ``market_date`` as the as-of date, and ``ftds`` carries the fail
date, an integer quantity and a string price. The seeded run, the page clock
and the row seeding come from the D1 render test.

Pins:
  - all four shipped families render, in ``RENDERED_FAMILIES`` order;
  - neither shorts family has a filing date, so the delay renders as
    "today minus as-of", labelled that way;
  - FTD is collapsed: one line for the whole window, the newest days listed
    under the profile cap, and the rest disclosed as a number;
  - a day whose amount is unknown is disclosed, never counted as zero;
  - adding the families changes nothing outside the bucket (R-DL1);
  - every generated string passes ``ensure_clean``.
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
    FTDS_PATH,
    SHORT_INTEREST_PATH,
    build_delayed_evidence,
    parse_ftds,
    run_delayed_job,
)
from webapp.board.delayed_panel import (
    RENDERED_FAMILIES,
    build_delayed_panel,
    load_delayed_panels,
)
from webapp.board.honesty import ensure_clean

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

_DELAYED = _SETTINGS.delayed

_SI_ROWS = [  # newest first, as live; si_float is a fraction
    {
        "symbol": "AAA", "short_interest": 74230933, "market_date": "2026-08-31",
        "short_shares_available": 10000000, "total_float": 3949547394,
        "si_float": "0.01879479484478873935497835426", "days_to_cover": "2.04",
        "fee_rate": "0.4081", "rebate_rate": "3.2219",
    },
    {
        "symbol": "AAA", "short_interest": 69196896, "market_date": "2026-08-14",
        "short_shares_available": 10000000, "total_float": 3949547394,
        "si_float": "0.01752020905107285313411787862", "days_to_cover": "2.15",
        "fee_rate": "0.2500", "rebate_rate": "3.3800",
    },
]

# Eight fail days inside the 60-day window (from 2026-07-17), newest first.
_FTD_ROWS = [
    {"date": day, "quantity": qty, "price": "200.00"}
    for day, qty in (
        ("2026-08-14", 1000), ("2026-08-11", 2000), ("2026-08-08", 3000), ("2026-08-05", 4000),
        ("2026-08-02", 5000), ("2026-07-30", 6000), ("2026-07-27", 7000), ("2026-07-24", 8000),
    )
]
_FTD_TOTAL_USD = "7,200,000"  # (1000 + ... + 8000) shares x $200.00


class _FakeClient:
    """Short interest and FTDs for AAA; every other family and ticker answers empty."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method, params
        if path == SHORT_INTEREST_PATH.format(ticker="AAA"):
            return {"data": _SI_ROWS}
        if path == FTDS_PATH.format(ticker="AAA"):
            return {"data": _FTD_ROWS}
        return {"data": []}


def _seed_delayed(engine: Engine) -> None:
    asyncio.run(
        run_delayed_job(_FakeClient(), engine, ["AAA", "BBB"], now=_NOW, settings=_DELAYED),  # type: ignore[arg-type]
    )
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as session:
        session.add_all([
            AlfaDailyClose(ticker="AAA", day=day, close=close, fetched_at=_NOW)
            for day, close in (
                (date(2026, 8, 14), 98.0), (date(2026, 8, 31), 100.0), (date(2026, 9, 14), 103.0),
            )
        ])
        session.commit()


@pytest.fixture
def board(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, TestClient]]:
    import webapp.board.delayed_panel as panel_module
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'shorts.db'}"
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


def _family(row: str, family: str) -> str:
    """The rendered block of one family, up to the next family block."""
    start = row.index(f'data-delayed-family="{family}"')
    rest = row[start:]
    nxt = rest.find("data-delayed-family=", 1)
    return rest if nxt == -1 else rest[:nxt]


def test_all_four_shipped_families_render_in_order(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    _seed_delayed(engine)
    row = _rows_of(client.get("/alfa").text)["AAA"]
    assert RENDERED_FAMILIES == ("congress", "insider", "short_interest", "ftd")
    assert re.findall(r'data-delayed-family="(\w+)"', row) == list(RENDERED_FAMILIES)
    assert [
        m for m in ("Short", "FTD") if f">{m}</span>" in row
    ] == ["Short", "FTD"]


def test_short_interest_is_labelled_as_of_with_a_today_minus_as_of_delay(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    short = _text(_family(_rows_of(client.get("/alfa").text)["AAA"], "short_interest"))

    assert "itibarıyla tarihi 2026-08-31" in short
    assert "bugün - itibarıyla tarihi = 15 gün (kaynakta bildirim tarihi yok)" in short
    assert "açığa satış / serbest dolaşım %1.88; short kapatma süresi 2.04 gün" in short
    assert "itibarıyla tarihinden beri dayanak %+3.0 (2026-08-31 → 2026-09-14 kapanış)" in short
    # Neither shorts source has a filing date, so no item here is dated as one. The delay
    # line says exactly that in words ("kaynakta bildirim tarihi yok"), so match a dated label.
    assert "bildirim tarihi 2026-" not in short


def test_ftd_is_collapsed_into_one_line_with_the_newest_days_listed(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    ftd = _family(_rows_of(client.get("/alfa").text)["AAA"], "ftd")
    text = _text(ftd)

    assert f"pencerede 8 FTD günü, toplam ≈ ${_FTD_TOTAL_USD}; en yenisi 2026-08-14" in text
    assert ftd.count("data-delayed-item=") == _DELAYED.max_items_per_family == 5
    assert "en yeni 5 kayıt gösteriliyor; 3 kayıt daha var" in text
    assert "FTD günü 2026-08-14" in text
    assert "bugün - FTD günü = 32 gün (kaynakta bildirim tarihi yok)" in text
    assert "1,000 hisse x $200.00 ≈ $200,000" in text
    assert "FTD gününden beri dayanak %+5.1 (2026-08-14 → 2026-09-14 kapanış)" in text


def test_a_day_with_an_unusable_amount_is_disclosed_never_counted_as_zero() -> None:
    rows = [*_FTD_ROWS[:2], {"date": "2026-08-01", "quantity": 500, "price": "0.0000"}]
    records = parse_ftds(rows, ticker="AAA", today=_TODAY, settings=_DELAYED).records
    items = build_delayed_evidence("AAA", records, (), today=_TODAY, settings=_DELAYED).items
    panel = build_delayed_panel("AAA", items, {}, settings=_DELAYED)
    ftd = next(f for f in panel.families if f.family == "ftd")

    assert ftd.summary_text == (
        "pencerede 3 FTD günü, bilinen tutarların toplamı ≈ $600,000 "
        "(1 günün tutarı bilinmiyor); en yenisi 2026-08-14"
    )


def test_an_unfetched_shorts_family_reads_bilinmiyor(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    body = client.get("/alfa").text
    for family in ("short_interest", "ftd"):
        assert body.count(f'data-delayed-family="{family}"') == len(_ROWS)
    assert body.count('data-delayed-state="never_fetched"') == len(_ROWS) * len(RENDERED_FAMILIES)
    assert "data-delayed-summary" not in body  # nothing to collapse, so nothing is claimed

    _seed_delayed(engine)
    bbb = _rows_of(client.get("/alfa").text)["BBB"]
    assert 'data-delayed-state="empty"' in _family(bbb, "ftd")
    assert "kayıt yok — kaynak yanıt verdi" in _text(_family(bbb, "short_interest"))


def test_adding_the_shorts_families_changes_nothing_outside_the_bucket(
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


def test_every_generated_shorts_string_is_clean(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    _seed_delayed(engine)
    assert client.get("/alfa").status_code == 200

    panels = load_delayed_panels(engine, ["AAA"], today=_TODAY, settings=_DELAYED)
    generated: list[str] = []
    for family in panels["AAA"].families:
        if family.family not in ("short_interest", "ftd"):
            continue
        generated += [family.label, *family.notes]
        generated += [t for t in (family.summary_text, family.omitted_text) if t is not None]
        for item in family.items:  # both shorts families generate their own size line
            generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text]
            generated += [t for t in (item.size_text,) if t is not None]
    assert len(generated) >= 20
    for text in generated:
        assert ensure_clean(text) == text
