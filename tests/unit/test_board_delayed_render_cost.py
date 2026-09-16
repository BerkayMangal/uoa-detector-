"""Phase 5.2.D-fix2: the delayed bucket only builds what the row shows.

Review finding FD-02; contract ``docs/phase-5.2-alfa-board-acceptance.md``
§4.1 (render budget: p95 <= 1.5 s at 2,000 signals) and §7.

Before this fix ``build_delayed_evidence`` built a full ``DelayedItem`` for
every stored row inside the lookback window — json.loads, six ``ensure_clean``
guards and two linear scans over the ticker's whole close history each — and
``_family_panel`` then threw all but ``delayed.max_items_per_family`` of them
away. ``alfa_delayed`` and ``alfa_daily_close`` are append-only, so that work
grows every day for a fixed display.

What is pinned here:
  - only the displayed items are built (the cap is applied to the RECORDS);
  - what the row shows is byte-for-byte what capping after the build showed:
    the same items, the same ``omitted`` count and the same collapsed FTD
    summary over the whole window, never over the displayed slice;
  - the uncapped builder is unchanged, so the single-ticker path and the FAZ C
    outcome readers keep every item;
  - the bisect close index answers exactly what the linear helper answered.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board import delayed as delayed_module
from webapp.board import delayed_panel as panel_module
from webapp.board.daily_close import (
    AlfaDailyClose,
    ClosePoint,
    build_close_index,
    close_on_or_before,
    ensure_daily_close_tables,
    load_closes_by_ticker,
    pct_move_between,
)
from webapp.board.db import make_engine, session_factory
from webapp.board.delayed import (
    AlfaDelayed,
    build_delayed_evidence,
    ensure_delayed_tables,
    load_delayed_records_by_ticker,
)
from webapp.board.delayed_coverage import coverage_by_family, load_coverage, record_fetch
from webapp.board.delayed_panel import RENDERED_FAMILIES, build_delayed_panel, load_delayed_panels
from webapp.board.settings import load_board_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed
_TODAY = date(2026, 9, 15)
_NOW = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)
_TICKERS = ("AAA", "BBB", "CCC")

# Live-shaped volume for one active ticker: FTD publishes a row per fail day,
# insider groups a Form 4 per filer and day, congress files for a year.
_FTD_DAYS = 42
_INSIDER_GROUPS = 60
_CONGRESS_FILINGS = 30
_PER_TICKER = _FTD_DAYS + _INSIDER_GROUPS + _CONGRESS_FILINGS
_CLOSE_DAYS = 400  # a year of stored closes, and it only grows


def _ftd(day: date, i: int) -> AlfaDelayed:
    return AlfaDelayed(
        ticker="", family="ftd", dedupe_key=f"ftd-{i}", filed_or_asof_date=day,
        transaction_date=None, delay_days=None, side=None, size_text=None,
        size_low=200_000.0, size_high=200_000.0, flag_late=None, flag_executive=None,
        flag_10b5_1=None, form=None,
        payload_json=json.dumps({"date": day.isoformat(), "quantity": 1000, "price": "200.00"}),
        fetched_at=_NOW,
    )


def _insider(day: date, i: int) -> AlfaDelayed:
    return AlfaDelayed(
        ticker="", family="insider", dedupe_key=f"ins-{i}", filed_or_asof_date=day,
        transaction_date=day - timedelta(days=2), delay_days=2, side="buy", size_text=None,
        size_low=50_000.0, size_high=50_000.0, flag_late=None, flag_executive=None,
        flag_10b5_1=False, form="4",
        payload_json=json.dumps({
            "ids": [f"{i}-a", f"{i}-b"], "amount": 100, "price": "500.00",
            "owner_name": "Jane Doe", "officer_title": "CFO",
        }),
        fetched_at=_NOW,
    )


def _congress(day: date, i: int) -> AlfaDelayed:
    return AlfaDelayed(
        ticker="", family="congress", dedupe_key=f"con-{i}", filed_or_asof_date=day,
        transaction_date=day - timedelta(days=50), delay_days=50, side="sell",
        size_text="$1,001 - $15,000", size_low=1001.0, size_high=15_000.0, flag_late=True,
        flag_executive=False, flag_10b5_1=None, form=None,
        payload_json=json.dumps({"name": "Al Green"}), fetched_at=_NOW,
    )


def _seed(engine: Engine) -> None:
    ensure_delayed_tables(engine)
    ensure_daily_close_tables(engine)
    factory = session_factory(engine)
    rows: list[AlfaDelayed] = []
    closes: list[AlfaDailyClose] = []
    for ticker in _TICKERS:
        for i in range(_FTD_DAYS):
            rows.append(_ftd(_TODAY - timedelta(days=i), i))
        for i in range(_INSIDER_GROUPS):
            rows.append(_insider(_TODAY - timedelta(days=i % 80), i))
        for i in range(_CONGRESS_FILINGS):
            rows.append(_congress(_TODAY - timedelta(days=i * 3), i))
        for row in rows[-_PER_TICKER:]:
            row.ticker = ticker
        closes += [
            AlfaDailyClose(
                ticker=ticker, day=_TODAY - timedelta(days=d), close=100.0 + d, fetched_at=_NOW,
            )
            for d in range(_CLOSE_DAYS)
        ]
        for family in RENDERED_FAMILIES:
            record_fetch(factory, ticker, family, status="ok", at=_NOW, truncated=False)
    with Session(engine) as session:
        session.add_all(rows)
        session.add_all(closes)
        session.commit()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    panel_module._tables_ready_for.clear()
    made = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    try:
        _seed(made)
        yield made
    finally:
        made.dispose()
        panel_module._tables_ready_for.clear()


# ---------------------------------------------------------------------------
# FD-02: the cap is applied before the work, not after
# ---------------------------------------------------------------------------


def test_only_the_displayed_items_are_built(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = sum(len(r) for r in load_delayed_records_by_ticker(engine, list(_TICKERS)).values())
    assert stored == _PER_TICKER * len(_TICKERS)  # every row is inside its window

    built: list[str] = []
    real = delayed_module._item

    def _counting(*args: Any, **kwargs: Any) -> Any:
        built.append(args[0].dedupe_key)
        return real(*args, **kwargs)

    monkeypatch.setattr(delayed_module, "_item", _counting)
    panels = load_delayed_panels(engine, list(_TICKERS), today=_TODAY, settings=_SETTINGS)

    shown = sum(len(f.items) for p in panels.values() for f in p.families)
    # Three families hold rows here; short interest holds none.
    assert shown == _SETTINGS.max_items_per_family * 3 * len(_TICKERS)
    assert len(built) == shown, f"built {len(built)} items to show {shown}"


def test_capping_early_shows_exactly_what_capping_late_showed(engine: Engine) -> None:
    """The reference path: every item built, then sliced at render (the 5.2.D1 behaviour)."""
    records = load_delayed_records_by_ticker(engine, ["AAA"])["AAA"]
    closes = load_closes_by_ticker(engine, ["AAA"])["AAA"]
    coverage = coverage_by_family(load_coverage(engine, ["AAA"]), "AAA")
    reference = build_delayed_panel(
        "AAA",
        build_delayed_evidence("AAA", records, closes, today=_TODAY, settings=_SETTINGS).items,
        coverage,
        settings=_SETTINGS,
    )

    live = load_delayed_panels(engine, ["AAA"], today=_TODAY, settings=_SETTINGS)["AAA"]
    assert live == reference


def test_the_collapsed_ftd_summary_still_covers_the_whole_window(engine: Engine) -> None:
    panels = load_delayed_panels(engine, ["AAA"], today=_TODAY, settings=_SETTINGS)
    ftd = next(f for f in panels["AAA"].families if f.family == "ftd")

    assert ftd.summary_text is not None
    assert f"pencerede {_FTD_DAYS} FTD günü" in ftd.summary_text  # not the displayed five
    assert f"${_FTD_DAYS * 200_000:,.0f}" in ftd.summary_text
    assert len(ftd.items) == _SETTINGS.max_items_per_family
    assert ftd.omitted == _FTD_DAYS - _SETTINGS.max_items_per_family
    assert ftd.omitted_text == (
        f"en yeni {_SETTINGS.max_items_per_family} kayıt gösteriliyor; {ftd.omitted} kayıt daha var"
    )


def test_the_uncapped_builder_still_returns_every_item(engine: Engine) -> None:
    records = load_delayed_records_by_ticker(engine, ["AAA"])["AAA"]
    evidence = build_delayed_evidence("AAA", records, (), today=_TODAY, settings=_SETTINGS)
    assert len(evidence.items) == _PER_TICKER


# ---------------------------------------------------------------------------
# The close lookup
# ---------------------------------------------------------------------------


def test_the_close_index_answers_what_the_linear_lookup_answered() -> None:
    base = date(2024, 1, 1)
    closes = tuple(
        ClosePoint(day=base + timedelta(days=i * 3), close=100.0 + i) for i in range(1000)
    )
    index = build_close_index(reversed(closes))  # input order does not matter

    probes = (base - timedelta(days=1), base, base + timedelta(days=1), base + timedelta(days=1499),
              base + timedelta(days=2997), base + timedelta(days=5000))
    for day in probes:
        assert index.on_or_before(day) == close_on_or_before(closes, day)
    for start in probes:
        for end in probes:
            assert index.pct_move_between(start, end) == pct_move_between(closes, start, end)


def test_the_close_index_of_an_empty_history_knows_nothing() -> None:
    index = build_close_index(())
    assert index.on_or_before(_TODAY) is None
    assert index.pct_move_between(_TODAY - timedelta(days=10), _TODAY) is None
