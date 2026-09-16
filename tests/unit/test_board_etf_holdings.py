"""Phase 5.2.B6a: focused-ETF holdings data layer (webapp/board/etf_holdings.py).

Rows are trimmed from the 2026-09-15 SMH and QQQ holdings probes; the cash row
follows the probed shape (null ticker, negative weight).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from webapp.board import etf_holdings as eh
from webapp.board.db import make_engine, session_factory
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)


def _row(ticker: str | None, weight: str, *, etf: str = "SMH", kind: str = "stock",
         updated: str = "2026-09-11", sector: str | None = "Technology") -> dict[str, Any]:
    return {"ticker": ticker, "type": kind, "weight": weight, "sector": sector,
            "updated": updated, "etf": etf, "short_name": ticker}


_SMH: dict[str, Any] = {"data": [
    _row("NVDA", "22.09"), _row("TSM", "9.75"), _row("AMD", "5.78"), _row("AVGO", "5.83"),
    _row("MU", "5.54"), _row("ASML", "4.97"),
    _row(None, "-0.152228", kind="cash", sector=None),
    _row(None, "1.0"),
    _row("BAD", "n/a"),
]}


class _FakeClient:
    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._responses = responses or {}
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        self.calls.append(path)
        if path in self._errors:
            raise self._errors[path]
        return self._responses.get(path, {"data": []})


def _path(etf: str) -> str:
    return eh.HOLDINGS_PATH.format(etf=etf)


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    eh.ensure_etf_holding_tables(engine)
    return session_factory(engine)


async def _refresh(sessions: Any, settings: BoardSettings, client: _FakeClient,
                   etfs: list[str] | None = None) -> eh.EtfHoldingsReport:
    return await eh.refresh_etf_holdings(
        client, sessions, etfs=etfs, settings=settings, now=_NOW,  # type: ignore[arg-type]
    )


def test_ensure_tables_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    eh.ensure_etf_holding_tables(engine)
    eh.ensure_etf_holding_tables(engine)
    assert "alfa_etf_holding" in inspect(engine).get_table_names()


async def test_default_list_is_the_profile_focused_etfs(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient()
    report = await _refresh(sessions, settings, client)
    assert client.calls == [_path(e) for e in settings.portfolio.focused_etfs]
    assert report.requests == len(settings.portfolio.focused_etfs)


async def test_keeps_stock_rows_with_ticker_and_percent_weights(
    sessions: Any, settings: BoardSettings,
) -> None:
    report = await _refresh(sessions, settings, _FakeClient({_path("SMH"): _SMH}), ["smh"])
    assert report.stored == (("SMH", 6),)
    with sessions() as s:
        rows = eh.load_focused_holdings(s)
    assert [r.ticker for r in rows] == ["AMD", "ASML", "AVGO", "MU", "NVDA", "TSM"]
    nvda = next(r for r in rows if r.ticker == "NVDA")
    assert nvda.weight_pct == pytest.approx(22.09)
    assert nvda.updated == date(2026, 9, 11)
    assert nvda.sector == "Technology"
    assert nvda.fetched_at == _NOW


async def test_share_classes_are_stored_as_reported_and_duplicates_add_up(
    sessions: Any, settings: BoardSettings,
) -> None:
    qqq = {"data": [
        _row("GOOGL", "3.151253", etf="QQQ", updated="2026-09-13"),
        _row("GOOG", "2.925557", etf="QQQ", updated="2026-09-13"),
        _row("MSFT", "3.0", etf="QQQ", updated="2026-09-13"),
        _row("MSFT", "2.881168", etf="QQQ", updated="2026-09-13"),
        _row("OTHER", "9", etf="SPY", updated="2026-09-13"),  # another fund's row, ignored
    ]}
    await _refresh(sessions, settings, _FakeClient({_path("QQQ"): qqq}), ["QQQ"])
    with sessions() as s:
        rows = {r.ticker: r.weight_pct for r in eh.load_focused_holdings(s)}
    assert rows == pytest.approx({"GOOG": 2.925557, "GOOGL": 3.151253, "MSFT": 5.881168})


async def test_broad_etf_is_skipped_and_its_old_rows_removed(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient({_path("SMH"): _SMH}), ["SMH"])
    cap = settings.portfolio.max_focused_holdings
    broad = {"data": [_row(f"T{i:03d}", "1.0") for i in range(cap + 1)]}
    report = await _refresh(sessions, settings, _FakeClient({_path("SMH"): broad}), ["SMH"])
    assert report.skipped_broad == (("SMH", cap + 1),)
    assert report.stored == ()
    with sessions() as s:
        assert eh.load_focused_holdings(s) == ()


async def test_new_snapshot_replaces_the_old_one(sessions: Any, settings: BoardSettings) -> None:
    await _refresh(sessions, settings, _FakeClient({_path("SMH"): _SMH}), ["SMH"])
    newer = {"data": [_row("NVDA", "21.5", updated="2026-09-14"), _row("TSM", "10", updated="2026-09-14")]}
    await _refresh(sessions, settings, _FakeClient({_path("SMH"): newer}), ["SMH"])
    with sessions() as s:
        rows = eh.load_focused_holdings(s)
    assert [(r.ticker, r.updated) for r in rows] == [
        ("NVDA", date(2026, 9, 14)), ("TSM", date(2026, 9, 14)),
    ]


async def test_not_found_and_empty_are_no_data_and_keep_rows(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh(sessions, settings, _FakeClient({_path("SMH"): _SMH}), ["SMH"])
    client = _FakeClient(
        {_path("IGV"): {"data": [_row(None, "1", kind="cash")]}},
        errors={_path("SMH"): UnusualWhalesNotFoundError("422", status_code=422)},
    )
    report = await _refresh(sessions, settings, client, ["SMH", "IGV"])
    assert report.no_data == ("SMH", "IGV")
    with sessions() as s:
        assert len(eh.load_focused_holdings(s)) == 6


@pytest.mark.parametrize(
    "error",
    [UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("500"),
     CircuitBreakerOpenError("open")],
)
async def test_degraded_etf_keeps_rows_and_the_job_continues(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    await _refresh(sessions, settings, _FakeClient({_path("SMH"): _SMH}), ["SMH"])
    igv = {"data": [_row("MSFT", "8.0", etf="IGV", sector="Technology")]}
    client = _FakeClient({_path("IGV"): igv}, errors={_path("SMH"): error})
    report = await _refresh(sessions, settings, client, ["SMH", "IGV"])
    assert report.degraded == ("SMH",)
    assert report.stored == (("IGV", 1),)
    with sessions() as s:
        assert len(eh.load_focused_holdings(s)) == 7


@pytest.mark.parametrize(
    "error", [UnusualWhalesDailyLimitError("daily_request_limit"), UnusualWhalesAuthError("401")],
)
async def test_daily_limit_and_auth_propagate(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient(errors={_path("SMH"): error})
    with pytest.raises(type(error)):
        await _refresh(sessions, settings, client, ["SMH", "IGV"])
    assert client.calls == [_path("SMH")]
