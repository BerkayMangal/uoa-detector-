"""Phase 5.2.A3: ticker info (``webapp/board/ticker_info.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 (daily jobs), §4.2
(``alfa_ticker_info``), §4.4 and §9 (Sektör ``kapsam-dışı`` only for ETF or
Index).

Fixture objects are trimmed from the live ``/api/stock/{ticker}/info``
responses captured 2026-09-15 (``alfa_probe/clusters_nvda_info.json``,
``clusters_smh_stock_info.json``).

Pins:
  - ``data`` is one object; issue type and sector are read, a null or blank
    sector stays null; anything else is rejected;
  - fetch: the probed path; not-found is stored as an empty row; rate limit,
    transient, open breaker and an unexpected payload are degraded;
    daily-limit and auth errors propagate;
  - only ETF and Index (case-insensitive) count as fund-or-index;
  - upsert and read, and which tickers are due for a fetch on a given day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.db import TABLE_PREFIX, make_engine
from webapp.board.ticker_info import (
    TICKER_INFO_PATH,
    AlfaTickerInfo,
    TickerInfoSnapshot,
    TickerInfoView,
    ensure_ticker_info_tables,
    fetch_ticker_info,
    parse_ticker_info,
    read_ticker_infos,
    tickers_needing_info,
    upsert_ticker_infos,
)

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

if TYPE_CHECKING:
    from pathlib import Path

_NVDA: dict[str, Any] = {
    "data": {
        "symbol": "NVDA", "beta": "1.9302", "full_name": "NVIDIA", "sector": "Technology",
        "issue_type": "Common Stock", "marketcap": "5084136000000", "uw_tags": ["semi", "gaming"],
    },
}
_SMH: dict[str, Any] = {
    "data": {
        "symbol": "SMH", "full_name": "VANECK SEMICONDUCTOR ETF", "sector": None,
        "issue_type": "ETF", "marketcap": "66248124771", "uw_tags": ["semi"],
    },
}
_DAY1 = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_DAY2 = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)


class _FakeClient:
    def __init__(self, *, payload: object = None, error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self._payload = payload
        self._error = error

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> Any:
        self.calls.append(path)
        if self._error is not None:
            raise self._error
        return self._payload


def test_parse_reads_issue_type_and_sector() -> None:
    assert parse_ticker_info(_NVDA, "nvda") == TickerInfoSnapshot("NVDA", "Common Stock", "Technology")
    assert parse_ticker_info(_SMH, "SMH") == TickerInfoSnapshot("SMH", "ETF", None)


def test_parse_blank_or_non_string_values_are_null() -> None:
    payload = {"data": {"issue_type": "  ", "sector": 7}}
    assert parse_ticker_info(payload, "X") == TickerInfoSnapshot("X", None, None)


@pytest.mark.parametrize("payload", [None, [], {"data": None}, {"data": [_NVDA["data"]]}])
def test_parse_rejects_anything_but_one_object(payload: object) -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_ticker_info(payload, "NVDA")


async def test_fetch_calls_the_probed_path() -> None:
    client = _FakeClient(payload=_SMH)
    fetch = await fetch_ticker_info(client, " smh ")
    assert client.calls == [TICKER_INFO_PATH.format(ticker="SMH")] == ["/api/stock/SMH/info"]
    assert (fetch.snapshot, fetch.degraded) == (TickerInfoSnapshot("SMH", "ETF", None), False)


async def test_fetch_not_found_is_an_empty_row() -> None:
    client = _FakeClient(error=UnusualWhalesNotFoundError("HTTP 404", status_code=404))
    fetch = await fetch_ticker_info(client, "ZZZZ")
    assert (fetch.snapshot, fetch.degraded) == (TickerInfoSnapshot("ZZZZ", None, None), False)


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(_FakeClient(error=UnusualWhalesRateLimitError("rate-limited (HTTP 429)")), id="rate-limit"),
        pytest.param(_FakeClient(error=UnusualWhalesTransientError("returned HTTP 503")), id="transient"),
        pytest.param(_FakeClient(error=CircuitBreakerOpenError("circuit breaker is open")), id="breaker-open"),
        pytest.param(_FakeClient(payload={"data": []}), id="unexpected-payload"),
    ],
)
async def test_fetch_degrades(client: _FakeClient) -> None:
    fetch = await fetch_ticker_info(client, "NVDA")
    assert (fetch.snapshot, fetch.degraded) == (None, True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_fetch_propagates_daily_limit_and_auth_errors(error: Exception) -> None:
    with pytest.raises(type(error)):
        await fetch_ticker_info(_FakeClient(error=error), "NVDA")


@pytest.mark.parametrize(
    ("issue_type", "fund_or_index"),
    [("ETF", True), ("Index", True), ("etf", True), (" INDEX ", True),
     ("Common Stock", False), ("ADR", False), (None, False)],
)
def test_only_etf_and_index_are_fund_or_index(issue_type: str | None, fund_or_index: bool) -> None:
    view = TickerInfoView(ticker="X", issue_type=issue_type, sector=None, fetched_at=_DAY1)
    assert view.fund_or_index is fund_or_index


def test_upsert_read_and_tickers_due(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'info.db'}")
    try:
        ensure_ticker_info_tables(engine)
        ensure_ticker_info_tables(engine)
        assert upsert_ticker_infos(engine, [parse_ticker_info(_NVDA, "NVDA")], fetched_at=_DAY1) == 1
        views = read_ticker_infos(engine, ["nvda", "SMH", ""])
        assert tickers_needing_info(engine, ["nvda", "SMH", "NVDA"], today=_DAY1.date()) == ["SMH"]
        assert tickers_needing_info(engine, ["NVDA", "SMH"], today=_DAY2.date()) == ["NVDA", "SMH"]
        upsert_ticker_infos(engine, [TickerInfoSnapshot("NVDA", "ADR", None)], fetched_at=_DAY2)
        replaced = read_ticker_infos(engine, ["NVDA"])["NVDA"]
        assert read_ticker_infos(engine, []) == {}
    finally:
        engine.dispose()
    assert set(views) == {"NVDA"}
    nvda = views["NVDA"]
    assert (nvda.issue_type, nvda.sector, nvda.fetched_at) == ("Common Stock", "Technology", _DAY1)
    assert (replaced.issue_type, replaced.sector, replaced.fetched_at.date()) == ("ADR", None, date(2026, 9, 16))
    assert AlfaTickerInfo.__tablename__.startswith(TABLE_PREFIX)
