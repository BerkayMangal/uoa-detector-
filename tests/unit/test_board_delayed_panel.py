"""Phase 5.2.D1: the ``ek kanıt (gecikmeli)`` panel and its fetch coverage (contract §7).

Hermetic. Congress fixture rows keep the shape of the live
``/api/congress/recent-trades`` response captured 2026-09-15 (alfa_probe). A
fake client and a tmp sqlite file stand in for UW and Postgres. No network, no
env.

Pins:
  - a family that was never fetched reads ``bilinmiyor``, never ``kayıt yok``
    (R-UN1): the coverage table is what tells the two apart;
  - a source that answered with nothing reads the frozen ``kayıt yok`` wording,
    with the date of the check;
  - a source that never answered stays unknown, and a failed newest attempt
    over stored rows is disclosed with the date those rows are as of;
  - the source's own truncation signal is disclosed;
  - the display cap comes from the profile, lists the newest items and
    discloses the rest as a number; nothing is deleted;
  - the panel types carry no evidence-count field (R-DL1);
  - the render path creates its tables on a fresh database and reads one query
    per table;
  - every generated string passes ``ensure_clean``.
"""

from __future__ import annotations

import asyncio
import string
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from webapp.board import delayed_panel as panel_module
from webapp.board.daily_close import (
    AlfaDailyClose,
    ClosePoint,
    ensure_daily_close_tables,
    load_closes_by_ticker,
)
from webapp.board.db import TABLE_PREFIX, make_engine, session_factory
from webapp.board.delayed import (
    BUCKET_LABEL,
    CONGRESS_RECENT_TRADES_PATH,
    EXCLUSION_NOTE,
    build_delayed_evidence,
    load_delayed_records_by_ticker,
    parse_congress_trades,
    run_delayed_job,
)
from webapp.board.delayed_coverage import (
    AlfaDelayedFetch,
    CoverageView,
    load_coverage,
    record_fetch,
)
from webapp.board.delayed_panel import (
    RENDERED_FAMILIES,
    DelayedFamilyPanel,
    DelayedPanel,
    build_delayed_panel,
    load_delayed_panels,
    unreadable_panel,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import DelayedSettings, load_board_settings

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS: DelayedSettings = load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed
_TODAY = date(2026, 9, 15)
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)  # 17:05 ET
_ATTEMPT = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)  # 17:00 ET, the delayed job's clock


def _row(**over: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Gilbert Cisneros", "ticker": "NVDA", "issuer": "undisclosed", "is_active": True,
        "notes": "NVIDIA Corporation - Common Stock", "transaction_date": "2026-08-18",
        "txn_type": "Sell", "politician_id": "739eca36-a8f3-4894-96b1-420354fe17b6",
        "amounts": "$1,001 - $15,000", "filed_at_date": "2026-09-11",
        "reporter": "Hon. Gilbert Cisneros", "member_type": "house",
    }
    base.update(over)
    return base


def _rows(n: int, ticker: str = "NVDA") -> list[dict[str, Any]]:
    """``n`` distinct filings of ``ticker``, newest first, all inside the 365-day window."""
    return [
        _row(
            ticker=ticker,
            transaction_date=(date(2026, 8, 20) - timedelta(days=7 * i)).isoformat(),
            filed_at_date=(date(2026, 9, 11) - timedelta(days=7 * i)).isoformat(),
            amounts=f"$1,00{i} - $15,000",
        )
        for i in range(n)
    ]


def _items(rows: list[dict[str, Any]], *, ticker: str = "NVDA", closes: tuple[ClosePoint, ...] = ()) -> Any:
    records = parse_congress_trades(rows, ticker=ticker, today=_TODAY, settings=_SETTINGS).records
    return build_delayed_evidence(ticker, records, closes, today=_TODAY, settings=_SETTINGS).items


def _coverage(
    *, status: str = "ok", answered: bool = True, truncated: bool = False,
    attempt: datetime = _ATTEMPT, success: datetime | None = None,
) -> dict[str, CoverageView]:
    return {
        "congress": CoverageView(
            ticker="NVDA", family="congress", last_attempt_at=attempt, last_status=status,
            last_success_at=(success or attempt) if answered else None, truncated=truncated,
        ),
    }


def _panel(rows: list[dict[str, Any]], coverage: dict[str, CoverageView], **kw: Any) -> DelayedPanel:
    settings = kw.pop("settings", _SETTINGS)
    return build_delayed_panel("nvda", _items(rows, **kw), coverage, settings=settings)


def _congress(panel: DelayedPanel) -> DelayedFamilyPanel:
    return next(f for f in panel.families if f.family == "congress")


class _FakeClient:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        key = f"{path}?ticker={(params or {}).get('ticker')}"
        return self.responses.get(key, self.responses.get(path, {"data": []}))


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'board.db'}")


# ---------------------------------------------------------------------------
# Coverage: "never asked" is not "nothing there" (R-UN1)
# ---------------------------------------------------------------------------


def test_a_never_fetched_family_reads_bilinmiyor_not_kayit_yok() -> None:
    family = _congress(_panel([], {}))
    assert family.state == "never_fetched"
    assert family.dimmed is True
    assert family.state_text == "bilinmiyor — bu aile hiç çekilmedi"
    assert "kayıt yok" not in (family.state_text or "")
    assert family.items == ()


def test_a_source_that_answered_with_nothing_reads_kayit_yok_with_its_date() -> None:
    family = _congress(_panel([], _coverage(status="no_data")))
    assert family.state == "empty"
    assert family.state_text == "kayıt yok — kaynak yanıt verdi, pencerede kayıt yok (son kontrol 2026-09-15)"
    assert family.dimmed is True  # nothing to show is still not a clean read


def test_a_source_that_never_answered_stays_unknown() -> None:
    family = _congress(_panel([], _coverage(status="degraded", answered=False)))
    assert family.state == "unanswered"
    assert family.state_text == "bilinmiyor — kaynak yanıt vermedi (son deneme 2026-09-15)"
    assert "kayıt yok" not in (family.state_text or "")


def test_stored_rows_older_than_the_coverage_table_say_the_check_date_is_unknown() -> None:
    family = _congress(_panel(_rows(1), {}))
    assert family.state == "items"
    assert family.notes == ("son kontrol tarihi bilinmiyor",)


def test_items_carry_the_date_of_their_last_check() -> None:
    assert _congress(_panel(_rows(1), _coverage())).notes == ("son kontrol 2026-09-15",)


def test_a_failed_newest_attempt_over_stored_rows_is_disclosed() -> None:
    coverage = _coverage(
        status="degraded", attempt=_ATTEMPT, success=datetime(2026, 9, 12, 21, 0, tzinfo=UTC),
    )
    family = _congress(_panel(_rows(1), coverage))
    assert family.state == "items"  # the stored rows are still shown, with their age
    assert family.notes == (
        "son kontrol 2026-09-15",
        "son deneme (2026-09-15) başarısız; kayıtlar 2026-09-12 itibarıyla",
    )


def test_a_truncated_page_is_disclosed() -> None:
    family = _congress(_panel(_rows(1), _coverage(truncated=True)))
    assert family.notes[-1] == (
        "kaynak, döndürdüğünden fazla kayıt olduğunu bildirdi; liste eksik olabilir"
    )


# ---------------------------------------------------------------------------
# Display cap
# ---------------------------------------------------------------------------


def test_the_display_cap_lists_the_newest_and_discloses_the_rest() -> None:
    family = _congress(_panel(_rows(8), _coverage()))
    assert _SETTINGS.max_items_per_family == 5
    assert len(family.items) == 5
    assert [i.filed_or_asof_date for i in family.items] == sorted(
        (i.filed_or_asof_date for i in family.items), reverse=True,
    )
    assert family.items[0].filed_or_asof_date == date(2026, 9, 11)  # the newest filing
    assert family.omitted == 3
    assert family.omitted_text == "en yeni 5 kayıt gösteriliyor; 3 kayıt daha var"


def test_the_display_cap_comes_from_the_profile() -> None:
    settings = _SETTINGS.model_copy(update={"max_items_per_family": 2})
    family = _congress(_panel(_rows(8), _coverage(), settings=settings))
    assert len(family.items) == 2
    assert family.omitted == 6
    assert family.omitted_text == "en yeni 2 kayıt gösteriliyor; 6 kayıt daha var"


def test_an_uncapped_family_discloses_nothing_extra() -> None:
    family = _congress(_panel(_rows(2), _coverage()))
    assert (family.omitted, family.omitted_text) == (0, None)


# ---------------------------------------------------------------------------
# R-DL1: the panel is never evidence
# ---------------------------------------------------------------------------


def test_panel_types_have_no_count_fields() -> None:
    """R-DL1: nothing in the bucket can be read as, or feed, an evidence count."""
    for view in (DelayedPanel, DelayedFamilyPanel):
        for f in fields(view):
            assert not f.name.startswith("n_"), (view.__name__, f.name)
            for word in ("count", "lehte", "aleyhte", "support", "against", "unknown", "score"):
                assert word not in f.name, (view.__name__, f.name)


def test_only_the_shipped_families_render() -> None:
    """Kongre (D1), İçeriden (D2), Short and FTD (D3)."""
    assert RENDERED_FAMILIES == ("congress", "insider", "short_interest", "ftd")
    panel = _panel(_rows(1), _coverage())
    assert [f.family for f in panel.families] == list(RENDERED_FAMILIES)
    assert [f.label for f in panel.families] == ["Kongre", "İçeriden", "Short", "FTD"]
    assert (panel.bucket_label, panel.exclusion_note) == (BUCKET_LABEL, EXCLUSION_NOTE)
    assert panel.bucket_label == "ek kanıt (gecikmeli)"
    assert panel.exclusion_note == (
        "gecikmeli veri: kanıt sayımına, güç etiketine ve karşı argümana girmez"
    )


def test_an_unreadable_panel_is_unknown_everywhere() -> None:
    panel = unreadable_panel("nvda")
    assert panel.ticker == "NVDA"
    for family in panel.families:
        assert family.state == "unreadable"
        assert family.dimmed is True
        assert family.items == ()
        assert family.state_text == "gecikmeli ek kanıt okunamadı; bu, kayıt yok demek değil"


# ---------------------------------------------------------------------------
# Database paths
# ---------------------------------------------------------------------------


def test_the_job_records_fetch_coverage_for_every_family(engine: Engine) -> None:
    client = _FakeClient({f"{CONGRESS_RECENT_TRADES_PATH}?ticker=NVDA": {"data": _rows(2)}})
    asyncio.run(run_delayed_job(client, engine, ["NVDA"], now=_NOW, settings=_SETTINGS))  # type: ignore[arg-type]

    assert AlfaDelayedFetch.__tablename__.startswith(TABLE_PREFIX)
    assert "alfa_delayed_fetch" in inspect(engine).get_table_names()
    coverage = load_coverage(engine, ["NVDA"])
    assert {family for _ticker, family in coverage} == {"congress", "insider", "short_interest", "ftd"}
    congress = coverage[("NVDA", "congress")]
    assert (congress.last_status, congress.answered, congress.truncated) == ("ok", True, False)
    assert congress.last_attempt_at == _NOW
    # The other families answered with nothing: that is "kayıt yok", not "never fetched".
    assert coverage[("NVDA", "insider")].last_status == "no_data"
    assert coverage[("NVDA", "insider")].answered is True


def test_a_degraded_attempt_never_refreshes_the_success_time(engine: Engine) -> None:
    ensure_daily_close_tables(engine)
    factory = session_factory(engine)
    from webapp.board.delayed import ensure_delayed_tables

    ensure_delayed_tables(engine)
    record_fetch(factory, "NVDA", "congress", status="ok", at=_ATTEMPT, truncated=True)
    later = _ATTEMPT + timedelta(days=1)
    record_fetch(factory, "NVDA", "congress", status="degraded", at=later, truncated=False)

    view = load_coverage(engine, ["nvda"])[("NVDA", "congress")]
    assert view.last_attempt_at == later
    assert view.last_success_at == _ATTEMPT  # the degraded attempt did not confirm anything
    assert view.last_attempt_failed is True
    assert view.truncated is True  # kept from the last answered attempt


def test_load_delayed_panels_creates_its_tables_on_a_fresh_database(engine: Engine) -> None:
    panels = load_delayed_panels(engine, ["NVDA"], today=_TODAY, settings=_SETTINGS)
    assert set(panels) == {"NVDA"}
    assert _congress(panels["NVDA"]).state == "never_fetched"
    names = set(inspect(engine).get_table_names())
    assert {"alfa_delayed", "alfa_delayed_fetch", "alfa_daily_close"} <= names


def test_load_delayed_panels_reads_every_ticker_in_one_query_per_table(engine: Engine) -> None:
    client = _FakeClient({
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker={t}": {"data": _rows(2, t)}
        for t in ("AAA", "BBB", "CCC")
    })
    asyncio.run(run_delayed_job(client, engine, ["AAA", "BBB", "CCC"], now=_NOW, settings=_SETTINGS))  # type: ignore[arg-type]
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as session:
        # The outcome needs a close on or before the filing date and a later one.
        session.add_all([
            AlfaDailyClose(ticker="AAA", day=date(2026, 9, 11), close=100.0, fetched_at=_NOW),
            AlfaDailyClose(ticker="AAA", day=date(2026, 9, 14), close=103.0, fetched_at=_NOW),
        ])
        session.commit()
    panel_module._tables_ready_for.clear()
    load_delayed_panels(engine, ["AAA"], today=_TODAY, settings=_SETTINGS)  # warm the table guard

    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        del conn, cursor, args
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        panels = load_delayed_panels(engine, ["AAA", "BBB", "CCC"], today=_TODAY, settings=_SETTINGS)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(statements) == 3, statements  # rows, closes, coverage
    assert set(panels) == {"AAA", "BBB", "CCC"}
    assert all(_congress(p).state == "items" for p in panels.values())
    assert _congress(panels["AAA"]).items[0].outcome.known is True
    assert _congress(panels["BBB"]).items[0].outcome.text == "bilinmiyor"  # no close stored


def test_batch_readers_group_by_ticker(engine: Engine) -> None:
    client = _FakeClient({
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=AAA": {"data": _rows(2, "AAA")},
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=BBB": {"data": []},
    })
    asyncio.run(run_delayed_job(client, engine, ["AAA", "BBB"], now=_NOW, settings=_SETTINGS))  # type: ignore[arg-type]
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as session:
        session.add_all([
            AlfaDailyClose(ticker="AAA", day=date(2026, 9, 11), close=100.0, fetched_at=_NOW),
            AlfaDailyClose(ticker="AAA", day=date(2026, 9, 14), close=103.0, fetched_at=_NOW),
        ])
        session.commit()

    records = load_delayed_records_by_ticker(engine, ["aaa", "BBB", "AAA"])
    assert {t: len(r) for t, r in records.items()} == {"AAA": 2, "BBB": 0}
    closes = load_closes_by_ticker(engine, ["AAA", "BBB"])
    assert [c.day for c in closes["AAA"]] == [date(2026, 9, 11), date(2026, 9, 14)]
    assert closes["BBB"] == ()
    assert load_delayed_records_by_ticker(engine, []) == {}
    assert load_closes_by_ticker(engine, []) == {}


# ---------------------------------------------------------------------------
# R-WD1
# ---------------------------------------------------------------------------


def test_every_generated_panel_string_is_clean() -> None:
    for template in panel_module._TEXT.values():
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
        ensure_clean(template.format(**dict.fromkeys(names, "7")))
    panels = [
        _panel(_rows(8), _coverage(truncated=True)),
        _panel([], {}),
        _panel([], _coverage(status="no_data")),
        _panel([], _coverage(status="degraded", answered=False)),
        unreadable_panel("NVDA"),
    ]
    generated: list[str] = []
    for panel in panels:
        generated += [panel.bucket_label, panel.exclusion_note]
        for family in panel.families:
            generated += [family.label, *family.notes]
            generated += [t for t in (family.state_text, family.omitted_text) if t is not None]
            for item in family.items:  # who and the filed range are vendor data, not generated copy
                generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text]
    assert len(generated) >= 30
    for text in generated:
        assert ensure_clean(text) == text
