"""Phase 5.2.PERF1: the Jinja environment keeps compiled templates.

``webapp/main.py`` used to run with ``templates.env.cache = None``. The board's
row template is pulled in with ``{% include "_alfa_row.html" %}`` from inside
the row loop, so an uncached environment lexed, parsed and compiled it once per
row: at 50 rows that was ~95% of the render (0.71 s → 0.09 s per render once
cached, 400 prints / 50 rows on the author's machine).

A compiled-template cache must be invisible: it caches the compiled template,
never the rendered text, so the page must say exactly the same thing and must
still change whenever a row input changes.

Pins:
  - the row template is compiled once, not once per row, and a second render
    compiles nothing;
  - a render from the warm cache is byte-identical to the same render from an
    environment with the cache switched off (the pre-PERF1 behaviour);
  - the render still changes with the quote, the quote age, the evidence
    states, the strength label, the gate mode and the selected run.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.db import make_engine
from webapp.board.quotes import QuoteSnapshot, ensure_quotes_tables, upsert_quotes

from tests.conftest import build_print
from tests.unit.test_alfa_route import (  # noqa: F401
    _LIVE,
    _SPECS,
    _row_html,
    _seed,
    board,
)
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_CLOCK = _TS + timedelta(minutes=5)  # the page's pinned wall clock (quote and print ages)
_SYMBOL = "SPY260918C00760000"  # the chain recorded on the SPY call 760 prints of _SPECS
_STRENGTH = re.compile(r'data-strength="([^"]+)"')
_ROW_TEMPLATE = "_alfa_row.html"


def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the board's clock so two renders differ only where their inputs differ."""
    import webapp.main as m

    monkeypatch.setattr(m, "_now", lambda: _CLOCK)


def _upsert_quote(url: str, *, fetched_at: datetime) -> None:
    """Write the dominant contract's quote, as the refresher writes it."""
    engine = make_engine(url)
    try:
        ensure_quotes_tables(engine)
        upsert_quotes(
            engine,
            [
                QuoteSnapshot(
                    option_symbol=_SYMBOL, ticker="SPY", nbbo_bid=0.39, nbbo_ask=0.41,
                    last_price=0.41, volume=500, open_interest=1000,
                    last_tape_time=_CLOCK - timedelta(minutes=4), returned=True,
                ),
            ],
            fetched_at=fetched_at,
        )
    finally:
        engine.dispose()


def _seed_scored(url: str, run_id: str, *, scores: bool) -> None:
    """One SPY call 760 print, with or without the legacy evidence scores."""
    store = SqliteBacktestStore(url, flush_threshold=2)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        pr = build_print(
            event_id=f"{run_id}-1", ts=_TS, ticker="SPY", option_type="call",
            strike="760", dte=3, premium="400000",
        )
        event = (
            EnrichedEvent(
                print=pr, combined_score_post_penalty=0.3,
                price_confirmation_score=1.0, sector_confirmation_score=0.7,
            )
            if scores
            else EnrichedEvent(print=pr, combined_score_post_penalty=0.3)
        )
        store.add(
            event,
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
    finally:
        store.close()


def _spy_row(client: TestClient, **params: str) -> str:
    return _row_html(client.get("/alfa", params=params).text, "SPY", "up")


def test_the_row_template_is_compiled_once_not_once_per_row(
    board: tuple[str, TestClient],  # noqa: F811  (the fixture is imported by name)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS)
    import webapp.main as m

    env = m.templates.env
    assert env.cache is not None, "the Jinja template cache must stay on (Phase 5.2.PERF1)"
    compiled: list[str] = []
    original = env.compile

    def _counting(source: Any, name: str | None = None, filename: str | None = None,
                  **kwargs: Any) -> Any:
        compiled.append(name or "<string>")
        return original(source, name, filename, **kwargs)

    monkeypatch.setattr(env, "compile", _counting)
    env.cache.clear()  # a cold environment, as a fresh process starts

    body = client.get("/alfa").text
    assert body.count("data-row ") >= 3  # the row loop ran several times
    assert compiled.count(_ROW_TEMPLATE) == 1, f"compiled per row: {compiled}"

    compiled.clear()
    assert client.get("/alfa").status_code == 200
    assert compiled == [], f"a warm render still compiled: {compiled}"


def test_a_warm_cache_renders_exactly_what_an_uncached_environment_renders(
    board: tuple[str, TestClient],  # noqa: F811  (the fixture is imported by name)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS)
    _upsert_quote(url, fetched_at=_CLOCK - timedelta(seconds=41))
    _pin_clock(monkeypatch)
    import webapp.main as m

    env = m.templates.env
    for params in ({}, {"gate": "off"}):
        warm = client.get("/alfa", params=params).text  # compiled templates from the cache
        saved = env.cache
        env.cache = None  # the pre-PERF1 environment: recompile every include, every row
        try:
            cold = client.get("/alfa", params=params).text
        finally:
            env.cache = saved
        assert cold == warm, f"the cache changed the page ({params})"
        assert "data-row " in warm


def test_the_quote_and_its_age_still_change_the_render(
    board: tuple[str, TestClient],  # noqa: F811  (the fixture is imported by name)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, _LIVE, _SPECS)
    _pin_clock(monkeypatch)

    unquoted = _spy_row(client, gate="off")
    _upsert_quote(url, fetched_at=_CLOCK - timedelta(seconds=41))
    fresh = _spy_row(client, gate="off")
    _upsert_quote(url, fetched_at=_CLOCK - timedelta(seconds=300))
    stale = _spy_row(client, gate="off")

    assert "kotasyon yok" in unquoted
    assert unquoted != fresh, "a quote must change the row"
    assert fresh != stale, "the quote age must change the row"
    assert "41 sn önce" in fresh
    assert "300 sn önce" in stale


def test_the_evidence_states_and_the_strength_still_change_the_render(
    board: tuple[str, TestClient],  # noqa: F811  (the fixture is imported by name)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed_scored(url, "run-plain", scores=False)
    _seed_scored(url, "run-scored", scores=True)
    _pin_clock(monkeypatch)

    plain = _spy_row(client, run="run-plain")
    scored = _spy_row(client, run="run-scored")

    assert plain != scored
    assert 'data-family-state="supporting"' in scored
    assert 'data-family-state="supporting"' not in plain
    assert _STRENGTH.findall(plain) != _STRENGTH.findall(scored)


def test_the_gate_mode_and_the_selected_run_still_change_the_render(
    board: tuple[str, TestClient],  # noqa: F811  (the fixture is imported by name)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, client = board
    _seed(url, "backtest-x", _SPECS[:1], ts=_TS - timedelta(days=7))
    _seed(url, _LIVE, _SPECS)
    _pin_clock(monkeypatch)

    gate_on = client.get("/alfa").text
    gate_off = client.get("/alfa", params={"gate": "off"}).text
    other_run = client.get("/alfa", params={"run": "backtest-x"}).text

    assert 'data-section="main"' in gate_on
    assert 'data-section="all"' in gate_off
    assert gate_on != gate_off, "the gate mode must change the page"
    assert gate_on != other_run, "the selected run must change the page"
