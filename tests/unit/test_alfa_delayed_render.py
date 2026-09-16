"""Phase 5.2.D1 (R-DL1): the delayed bucket renders and changes nothing else.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-DL1 and R-UN1, §7.

The same seeded run is rendered twice: once with an empty ``alfa_delayed``
table, then again after the delayed job stored rows that would move every
number on the row if they were counted. Everything outside the bucket must be
byte-identical — the L/A/U counts, the strength label, the clean-candidate
flags, the ``Bugün temiz aday yok`` banner, the ``AMA`` choice and the row
order.

Vendor data. A filer's real name is not generated copy: "Al Green" matches the
standalone ``al`` pattern of ``webapp.board.honesty``, so it must never pass
through a raising guard at runtime, and the render-level forbidden-word scan
excludes the vendor spans. Vendor text is escaped, never rendered as markup.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.copy_tr import NO_CLEAN_CANDIDATE
from webapp.board.daily_close import AlfaDailyClose, ensure_daily_close_tables
from webapp.board.db import make_engine, session_factory
from webapp.board.delayed import CONGRESS_RECENT_TRADES_PATH, run_delayed_job
from webapp.board.delayed_panel import RENDERED_FAMILIES, load_delayed_panels
from webapp.board.evidence import STAGE_BY_FAMILY
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.netprem import ensure_netprem_tables
from webapp.board.quotes import (
    DepthSnapshot,
    QuoteSnapshot,
    ensure_quotes_tables,
    upsert_depths,
    upsert_quotes,
)
from webapp.board.settings import load_board_settings
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry, ensure_telemetry_tables
from webapp.board.ticker_info import ensure_ticker_info_tables

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # 10:00 ET, inside the session
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)  # 17:05 ET, after the post-close job
_TODAY = date(2026, 9, 15)

_ARTICLE = re.compile(
    r'<article[^>]*data-row data-ticker="([^"]+)" data-direction="([^"]+)"[^>]*>(.*?)</article>', re.S,
)
_DELAYED_BLOCK = re.compile(r"<details[^>]*data-delayed>.*?</details>", re.S)
_VENDOR_SPAN = re.compile(r'<span[^>]*data-delayed-(?:who|size)>[^<]*</span>')
_ROW_ORDER = re.compile(r'data-row data-ticker="([^"]+)" data-direction="([^"]+)"')

# AAA: one lehte family, İŞLENİR, five unknown -> not clean (max_unknown is 2).
# BBB: one aleyhte family, İŞLENMEZ (17% spread) -> not clean.
# No clean candidate, so the R-EM1 banner renders in both passes.
_ROWS = (("AAA", "300000", 0.4321, "strong", (0.39, 0.41)), ("BBB", "200000", 0.6137, "contrarian", (1.83, 2.17)))

# A real filer name that trips the standalone "al" pattern, plus a hostile-input
# fixture (not a real name) pinning that vendor text is escaped, never markup.
_AL_GREEN = "Al Green"
_MARKUP_NAME = "Al Green <b>&</b>"


def _congress_row(**over: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Gilbert Cisneros", "ticker": "AAA", "issuer": "undisclosed",
        "notes": "AAA Corporation - Common Stock", "transaction_date": "2026-08-18",
        "txn_type": "Sell", "politician_id": "739eca36-a8f3-4894-96b1-420354fe17b6",
        "amounts": "$1,001 - $15,000", "filed_at_date": "2026-09-11",
        "reporter": "Hon. Gilbert Cisneros", "member_type": "house",
    }
    base.update(over)
    return base


def _aaa_filings() -> list[dict[str, Any]]:
    """Eight filings: the newest two carry the vendor-name fixtures, three fall past the cap."""
    older = [
        _congress_row(
            politician_id=f"pol-{i}", name=f"Filer {i}", reporter=f"Hon. Filer {i}",
            transaction_date=(date(2026, 8, 1) - timedelta(days=9 * i)).isoformat(),
            filed_at_date=(date(2026, 9, 4) - timedelta(days=9 * i)).isoformat(),
            amounts=f"$15,00{i} - $50,000",
        )
        for i in range(6)
    ]
    return [
        _congress_row(
            name=_AL_GREEN, reporter=_AL_GREEN, politician_id="pol-al-green",
            txn_type="Buy", transaction_date="2026-07-14", filed_at_date="2026-09-11",
            amounts="$50,001 - $100,000",
        ),
        _congress_row(
            name=_MARKUP_NAME, reporter=_MARKUP_NAME, politician_id="pol-markup",
            txn_type="Buy", transaction_date="2026-08-30", filed_at_date="2026-09-10",
        ),
        *older,
    ]


class _FakeClient:
    """Answers congress for AAA, nothing for BBB; every other family answers empty."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        ticker = (params or {}).get("ticker")
        if path == CONGRESS_RECENT_TRADES_PATH and ticker == "AAA":
            return {"data": _aaa_filings()}
        return {"data": []}


def _symbol(ticker: str) -> str:
    return f"{ticker}260918C00100000"


def _seed_run(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(_ROWS) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (ticker, premium, score, _branch, _quote) in enumerate(_ROWS):
            pr = build_print(
                event_id=f"e{i}", ts=_TS + timedelta(seconds=i), ticker=ticker,
                option_type="call", strike="100", dte=3, premium=premium,
            )
            store.add(
                EnrichedEvent(print=pr, combined_score_post_penalty=score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        for ensure in (
            ensure_telemetry_tables, ensure_quotes_tables, ensure_netprem_tables,
            ensure_ticker_info_tables,
        ):
            ensure(engine)
        with Session(engine) as session:
            for i, (ticker, _premium, _score, branch, _quote) in enumerate(_ROWS):
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=f"e{i}", ticker=ticker, option_chain=_symbol(ticker),
                        fill_side="at_ask", option_type="call", strike="100",
                        expiry=(_TS + timedelta(days=3)).date(), print_ts=_TS, written_at=_TS,
                    ),
                )
                session.add(
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id=f"e{i}", stage_name=STAGE_BY_FAMILY["sector"],
                        branch=branch, metadata_json=None, degraded=False,
                        profile_content_hash="h", written_at=_TS,
                    ),
                )
            session.commit()
        upsert_quotes(
            engine,
            [
                QuoteSnapshot(
                    option_symbol=_symbol(t), ticker=t, nbbo_bid=q[0], nbbo_ask=q[1], last_price=q[1],
                    volume=500, open_interest=1000, last_tape_time=_TS, returned=True,
                )
                for t, _p, _s, _b, q in _ROWS
            ],
            fetched_at=_NOW - timedelta(seconds=41),
        )
        upsert_depths(
            engine,
            [
                DepthSnapshot(
                    option_symbol=_symbol(t), nbbo_bid_size=137, nbbo_ask_size=108,
                    nbbo_bid_time=_TS, nbbo_ask_time=_TS,
                )
                for t, _p, _s, _b, _q in _ROWS
            ],
            fetched_at=_NOW - timedelta(seconds=60),
        )
    finally:
        engine.dispose()


def _seed_delayed(engine: Engine) -> None:
    """Run the real post-close job against a fake client, then store the closes it reads."""
    asyncio.run(
        run_delayed_job(_FakeClient(), engine, ["AAA", "BBB"], now=_NOW, settings=_SETTINGS.delayed),  # type: ignore[arg-type]
    )
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as session:
        session.add_all([
            AlfaDailyClose(ticker="AAA", day=day, close=close, fetched_at=_NOW)
            for day, close in ((date(2026, 9, 11), 100.0), (date(2026, 9, 14), 103.0))
        ])
        session.commit()


def _reset(module: Any) -> None:
    module._REPO = module._JOURNAL = module._GAMMA = None
    module._BOARD_READER = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, TestClient]]:
    import webapp.board.delayed_panel as panel_module
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'delayed.db'}"
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


def _rows_of(body: str) -> dict[str, str]:
    return {ticker: inner for ticker, _direction, inner in _ARTICLE.findall(body)}


def _without_delayed(body: str) -> str:
    return _DELAYED_BLOCK.sub("", body)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


# ---------------------------------------------------------------------------
# R-DL1: the bucket renders, and nothing else moves
# ---------------------------------------------------------------------------


def test_delayed_rows_change_nothing_outside_their_bucket(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    before = client.get("/").text
    assert NO_CLEAN_CANDIDATE in before  # R-EM1: no row qualifies, in both passes

    _seed_delayed(engine)
    after = client.get("/").text

    assert "data-delayed-item=" not in before
    assert "data-delayed-item=" in after
    assert _without_delayed(before) == _without_delayed(after)


def test_counts_strength_clean_flags_banner_ama_and_order_are_identical(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    before = client.get("/").text
    _seed_delayed(engine)
    after = client.get("/").text

    def facts(body: str) -> dict[str, Any]:
        rows = _rows_of(body)
        return {
            "order": _ROW_ORDER.findall(body),
            "words": {t: re.findall(r"data-evidence-word>([^<]*)<", r) for t, r in rows.items()},
            "strength": {t: re.findall(r'data-strength="(\w+)">([^<]*)<', r) for t, r in rows.items()},
            "clean": re.findall(r'data-clean-candidate="(\w+)"', body),
            "banner": NO_CLEAN_CANDIDATE in body,
            "counter": {t: re.findall(r"<p data-counter>([^<]*)</p>", r) for t, r in rows.items()},
        }

    assert facts(before) == facts(after)
    # The seeded rows are the ones that would move if the bucket were counted.
    assert [html.unescape(w) for w in facts(after)["words"]["AAA"]] == [
        "1 lehte · 0 aleyhte · 5 bilinmiyor",
    ]
    assert facts(after)["clean"] == ["false", "false"]
    assert facts(after)["order"] == [("AAA", "up"), ("BBB", "up")]
    assert len(_rows_of(after)["AAA"].split("data-delayed-item=")) - 1 == 5  # eight filings, cap 5


def test_the_bucket_is_a_sibling_of_the_audit_block_not_nested_in_it(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    row = _rows_of(client.get("/").text)["AAA"]
    audit = re.search(r"<details[^>]*data-audit[^>]*>.*?</details>", row, re.S)
    assert audit is not None
    assert "data-delayed" not in audit.group(0)
    assert row.index("data-delayed>") < row.index("data-audit")


# ---------------------------------------------------------------------------
# R-UN1: never fetched is unknown, fetched-and-empty is "kayıt yok"
# ---------------------------------------------------------------------------


def test_a_fresh_database_reads_bilinmiyor_and_never_an_empty_clean_block(
    board: tuple[Engine, TestClient],
) -> None:
    _engine, client = board
    body = client.get("/").text
    assert body.count('data-delayed-state="never_fetched"') == len(_ROWS) * len(RENDERED_FAMILIES)
    assert "bilinmiyor — bu aile hiç çekilmedi" in html.unescape(body)
    assert "kayıt yok" not in html.unescape(body)
    assert body.count("border-dashed") >= len(_ROWS)  # dashed and dimmed, never clean


def test_a_fetched_family_with_no_rows_reads_kayit_yok(board: tuple[Engine, TestClient]) -> None:
    engine, client = board
    _seed_delayed(engine)
    rows = _rows_of(client.get("/").text)
    assert 'data-delayed-state="empty"' in rows["BBB"]  # the source answered with nothing
    assert "kayıt yok — kaynak yanıt verdi" in _text(rows["BBB"])
    assert 'data-delayed-state="items"' in rows["AAA"]
    assert "son kontrol 2026-09-15" in _text(rows["AAA"])


def test_items_show_the_filing_date_delay_flags_and_outcome(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    aaa = _text(_rows_of(client.get("/").text)["AAA"])
    assert "ek kanıt (gecikmeli)" in aaa
    assert "gecikmeli veri: kanıt sayımına, güç etiketine ve karşı argümana girmez" in aaa
    assert "bildirim tarihi 2026-09-11" in aaa
    assert "işlemden 59 gün sonra bildirildi" in aaa  # 2026-07-14 -> 2026-09-11
    assert "geç bildirim" in aaa  # above the 45-day STOCK Act cutoff
    assert "bildirim tarihinden beri dayanak %+3.0 (2026-09-11 → 2026-09-14 kapanış)" in aaa
    assert "en yeni 5 kayıt gösteriliyor; 3 kayıt daha var" in aaa


# ---------------------------------------------------------------------------
# Vendor data (rule 16)
# ---------------------------------------------------------------------------


def test_a_politician_named_al_green_renders_escaped_and_never_raises(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    response = client.get("/")
    assert response.status_code == 200
    aaa = _rows_of(response.text)["AAA"]

    # The trap: a real filer name matches the standalone "al" advisory pattern.
    assert forbidden_words(_AL_GREEN) == ["al"]
    assert f"data-delayed-who>{_AL_GREEN}</span>" in aaa
    # Vendor text is escaped, never markup, and the filed range is shown as filed.
    assert "Al Green &lt;b&gt;&amp;&lt;/b&gt;" in aaa
    assert "<b>" not in aaa
    assert "$50,001 - $100,000" in aaa


def test_generated_delayed_copy_stays_clean_with_vendor_names_on_the_page(
    board: tuple[Engine, TestClient],
) -> None:
    engine, client = board
    _seed_delayed(engine)
    body = client.get("/").text

    # The render-level scan excludes the vendor spans (rule 16); everything else is generated.
    assert forbidden_words(html.unescape(_VENDOR_SPAN.sub("", body))) == []
    panels = load_delayed_panels(engine, ["AAA", "BBB"], today=_TODAY, settings=_SETTINGS.delayed)
    generated: list[str] = []
    for panel in panels.values():
        generated += [panel.bucket_label, panel.exclusion_note]
        for family in panel.families:
            generated += [family.label, *family.notes]
            generated += [t for t in (family.state_text, family.omitted_text) if t is not None]
            for item in family.items:  # who and the filed range are vendor data
                generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text]
                generated += [*item.flags, *( [item.side_text] if item.side_text else [] )]
    assert len(generated) >= 20
    for text in generated:
        assert ensure_clean(text) == text


def test_a_failed_delayed_read_renders_unknown_not_an_empty_bucket(
    board: tuple[Engine, TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, client = board
    _seed_delayed(engine)
    import webapp.board.alfa_page as page_module

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        msg = "database unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(page_module, "load_delayed_panels", _boom)
    body = client.get("/").text
    assert body.count('data-delayed-state="unreadable"') == len(_ROWS) * len(RENDERED_FAMILIES)
    assert "gecikmeli ek kanıt okunamadı; bu, kayıt yok demek değil" in html.unescape(body)
    assert "data-delayed-item=" not in body


def test_the_board_still_makes_zero_unusual_whales_calls(
    board: tuple[Engine, TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

    engine, client = board
    _seed_delayed(engine)
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a page render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    for path in ("/", "/alfa"):
        assert client.get(path).status_code == 200
    assert calls == []
