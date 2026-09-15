"""Phase 5.2.B4a: board-side catalyst reader and chip (webapp/board/catalysts.py).

Fixtures are trimmed from the 2026-09-15 probes: MU earnings, IONS FDA calendar with
target_date_min, and the economic calendar (FOMC typed ``report``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from webapp.board import catalysts as cat
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import ensure_clean
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
_NOW = datetime(2026, 9, 15, 15, 35, tzinfo=UTC)  # 11:35 ET

_EARNINGS_MU: dict[str, Any] = {"data": [
    {"source": "company", "report_time": "postmarket", "report_date": "2026-09-30",
     "expected_move_perc": "0.0919", "actual_eps": None},
    {"source": "company", "report_time": "postmarket", "report_date": "2026-06-24",
     "expected_move_perc": "0.094227", "actual_eps": "24.89"},
]}

_FDA_IONS: dict[str, Any] = {"data": [
    {"ticker": "IONS", "catalyst": "PDUFA Date", "event_type": "PDUFA Date",
     "drug": "Zilganersen", "target_date": "2026-09-22", "status": "NDA Priority Review"},
    {"ticker": "IONS", "catalyst": "Top-line data", "drug": "ION582",
     "target_date": "2027-H2", "status": "Unknown"},
    {"ticker": "IONS", "catalyst": "Topline Readout", "drug": "X-1", "target_date": "2026-Q3"},
    {"ticker": "IONS", "catalyst": "Endpoint Met", "drug": "X-2", "target_date": ""},
    {"ticker": "IONS", "catalyst": "PDUFA Date", "event_type": "PDUFA Date",
     "drug": "Zilganersen", "target_date": "2026-09-22", "status": "duplicate row"},
    {"ticker": "OTHER", "catalyst": "PDUFA Date", "drug": "Y", "target_date": "2026-09-20"},
]}

_ECON: dict[str, Any] = {"data": [
    {"type": "report", "time": "2026-09-25T12:30:00Z", "event": "Durable Goods"},
    {"type": "report", "time": "2026-09-17T12:30:00Z", "event": "Weekly Jobless Claims"},
    {"type": "report", "time": "2026-09-16T18:00:00Z", "event": "U.S. interest rate decision"},
    {"type": "report", "time": "2026-09-16T18:00:00Z",
     "event": "Federal Reserve economic projections"},
    {"type": "report", "time": "2026-09-16T12:30:00Z", "event": "Retail Sales"},
    {"type": "report", "time": "not-a-time", "event": "FOMC minutes"},
]}


class _FakeClient:
    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._responses = responses or {}
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self._errors:
            raise self._errors[path]
        return self._responses.get(path, {"data": []})


def _earn(ticker: str) -> str:
    return cat.EARNINGS_PATH.format(ticker=ticker)


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    cat.ensure_catalyst_tables(engine)
    return session_factory(engine)


def _expiry_close(day: date) -> datetime:
    # 16:00 EDT == 20:00 UTC for September dates.
    return datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC)


def _chip(sessions: Any, settings: BoardSettings, ticker: str, end: datetime,
          start: datetime = _NOW) -> cat.CatalystChip:
    with sessions() as s:
        return cat.read_catalyst_chip(s, ticker=ticker, window_start=start, window_end=end,
                                      settings=settings)


async def _refresh_all(sessions: Any, settings: BoardSettings, ticker: str,
                       client: _FakeClient) -> cat.CatalystRefreshReport:
    return await cat.refresh_catalysts(
        client, sessions, tickers=[ticker], settings=settings, now=_NOW,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# fetch and store
# ---------------------------------------------------------------------------


def test_ensure_tables_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    cat.ensure_catalyst_tables(engine)
    cat.ensure_catalyst_tables(engine)
    assert {"alfa_catalyst", "alfa_catalyst_fetch"} <= set(inspect(engine).get_table_names())


async def test_refresh_sends_the_contract_queries(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient({_earn("MU"): _EARNINGS_MU, cat.ECONOMIC_CALENDAR_PATH: _ECON})
    report = await _refresh_all(sessions, settings, "mu", client)
    assert client.calls == [
        (_earn("MU"), None),
        (cat.FDA_PATH, {"ticker": "MU", "target_date_min": "2026-09-15"}),
        (cat.ECONOMIC_CALENDAR_PATH, None),
    ]
    assert report.requests == 3
    assert set(report.stored) == {("earnings", "MU", 1), ("fda", "MU", 0), ("macro", "*", 2)}


async def test_earnings_upcoming_row_maps_postmarket_after_the_close(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _refresh_all(sessions, settings, "MU", _FakeClient({_earn("MU"): _EARNINGS_MU}))
    with sessions() as s:
        events, _ = cat.load_catalyst_inputs(s, "MU")
    (earn,) = [e for e in events if e.kind == "earnings"]
    assert earn.when_key == "2026-09-30"
    assert earn.timing == "postmarket" and earn.estimated is False
    assert earn.starts_at == datetime(2026, 9, 30, 20, 0, tzinfo=UTC)  # 16:00 ET
    assert earn.ends_at == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    # Expiry 09-30: the postmarket report falls after the last close.
    assert _chip(sessions, settings, "MU", _expiry_close(date(2026, 9, 30))).parts[0].state == "yok"
    oct2 = _chip(sessions, settings, "MU", _expiry_close(date(2026, 10, 2)))
    assert oct2.parts[0].state == "var"
    assert oct2.parts[0].text == "Kazanç: 30.09 kapanış sonrası"


async def test_estimated_unknown_time_report_is_marked_and_conservative(
    sessions: Any, settings: BoardSettings,
) -> None:
    rows = {"data": [
        {"source": "estimation", "report_time": "unknown", "report_date": "2026-10-29"},
        {"source": "estimation", "report_time": None, "report_date": "2026-10-29"},
    ]}
    await _refresh_all(sessions, settings, "AAPL", _FakeClient({_earn("AAPL"): rows}))
    chip = _chip(sessions, settings, "AAPL", _expiry_close(date(2026, 10, 29)))
    earnings = chip.parts[0]
    assert earnings.state == "var"  # unknown time on expiry day could be before the close
    assert earnings.text == "Kazanç: 29.10 saati bilinmiyor (tahmini)"


async def test_known_timing_wins_over_unknown_for_the_same_day(
    sessions: Any, settings: BoardSettings,
) -> None:
    rows = {"data": [
        {"source": "company", "report_time": "unknown", "report_date": "2026-10-20"},
        {"source": "company", "report_time": "premarket", "report_date": "2026-10-20"},
    ]}
    await _refresh_all(sessions, settings, "NVDA", _FakeClient({_earn("NVDA"): rows}))
    chip = _chip(sessions, settings, "NVDA", _expiry_close(date(2026, 10, 20)))
    assert chip.parts[0].text == "Kazanç: 20.10 açılış öncesi"


async def test_fda_precise_dates_count_and_vague_labels_read_unclear(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({cat.FDA_PATH: _FDA_IONS})
    await _refresh_all(sessions, settings, "IONS", client)
    with sessions() as s:
        events, _ = cat.load_catalyst_inputs(s, "IONS")
    fda = [e for e in events if e.kind == "fda"]
    assert sorted((e.when_key, e.precision) for e in fda) == [
        ("2026-09-22", "day"), ("2026-Q3", "vague"), ("2027-H2", "vague"),
    ]
    wide = _chip(sessions, settings, "IONS", _expiry_close(date(2026, 9, 25)))
    part = wide.parts[1]
    assert part.state == "var"
    assert part.text == "FDA: 22.09 · zamanı belirsiz (2026-Q3)"
    short = _chip(sessions, settings, "IONS", _expiry_close(date(2026, 9, 18)))
    assert short.parts[1].state == "zamani_belirsiz"
    assert short.parts[1].text == "FDA: zamanı belirsiz (2026-Q3)"
    far = _chip(sessions, settings, "IONS", _expiry_close(date(2026, 9, 18)) + timedelta(days=500),
                start=datetime(2027, 1, 2, tzinfo=UTC))
    assert far.parts[1].vague_events[0].when_key == "2027-H2"


async def test_macro_is_matched_by_name_not_type(sessions: Any, settings: BoardSettings) -> None:
    await _refresh_all(sessions, settings, "SPY", _FakeClient({cat.ECONOMIC_CALENDAR_PATH: _ECON}))
    chip = _chip(sessions, settings, "SPY", _expiry_close(date(2026, 9, 18)))
    macro = chip.parts[2]
    assert macro.state == "var"
    assert [e.title for e in macro.events] == [
        "Federal Reserve economic projections", "U.S. interest rate decision",
    ]
    assert macro.text == (
        "Makro: economic projections 16.09 14:00 ET, interest rate decision 16.09 14:00 ET"
    )
    assert chip.in_window is True
    # The same rows are read for every ticker.
    assert _chip(sessions, settings, "QQQ", _expiry_close(date(2026, 9, 18))).parts[2].state == "var"


async def test_macro_beyond_horizon_reads_unknown(sessions: Any, settings: BoardSettings) -> None:
    await _refresh_all(sessions, settings, "SPY", _FakeClient({cat.ECONOMIC_CALENDAR_PATH: _ECON}))
    horizon = settings.catalyst.macro_horizon_days
    after_fomc = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    far_end = _NOW + timedelta(days=horizon + 20)
    none_but_far = _chip(sessions, settings, "SPY", far_end, start=datetime(2026, 9, 26, tzinfo=UTC))
    assert none_but_far.parts[2].state == "bilinmiyor"
    assert none_but_far.parts[2].beyond_horizon is True
    assert none_but_far.parts[2].text == "Makro: 25.09 sonrası bilinmiyor"
    with_hit = _chip(sessions, settings, "SPY", far_end)
    assert with_hit.parts[2].state == "var" and with_hit.parts[2].beyond_horizon is True
    inside = _chip(sessions, settings, "SPY", after_fomc + timedelta(hours=1), start=after_fomc)
    assert inside.parts[2].state == "yok"
    assert inside.parts[2].text == "Makro: yok"


async def test_never_fetched_reads_unknown_not_none(sessions: Any, settings: BoardSettings) -> None:
    chip = _chip(sessions, settings, "TSLA", _expiry_close(date(2026, 9, 18)))
    assert [p.state for p in chip.parts] == ["bilinmiyor", "bilinmiyor", "bilinmiyor"]
    assert chip.text == "Vade içinde katalizör: Kazanç: bilinmiyor · FDA: bilinmiyor · Makro: bilinmiyor"
    assert chip.in_window is False


# ---------------------------------------------------------------------------
# UW errors
# ---------------------------------------------------------------------------


async def test_not_found_is_no_data_and_clears_rows(sessions: Any, settings: BoardSettings) -> None:
    await _refresh_all(sessions, settings, "MU", _FakeClient({_earn("MU"): _EARNINGS_MU}))
    client = _FakeClient(errors={_earn("MU"): UnusualWhalesNotFoundError("404", status_code=404)})
    report = await cat.refresh_earnings(
        client, sessions, tickers=["MU"], settings=settings, now=_NOW + timedelta(days=1),  # type: ignore[arg-type]
    )
    assert report.no_data == (("earnings", "MU"),)
    chip = _chip(sessions, settings, "MU", _expiry_close(date(2026, 10, 2)))
    assert chip.parts[0].state == "yok"


@pytest.mark.parametrize(
    "error",
    [UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("500"),
     CircuitBreakerOpenError("open")],
)
async def test_degraded_keeps_previous_rows_and_continues(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    await _refresh_all(sessions, settings, "MU", _FakeClient({_earn("MU"): _EARNINGS_MU}))
    client = _FakeClient({cat.ECONOMIC_CALENDAR_PATH: _ECON}, errors={_earn("MU"): error})
    report = await _refresh_all(sessions, settings, "MU", client)
    assert report.degraded == (("earnings", "MU"),)
    assert len(client.calls) == 3
    chip = _chip(sessions, settings, "MU", _expiry_close(date(2026, 10, 2)))
    assert chip.parts[0].state == "var"
    with sessions() as s:
        _, fetches = cat.load_catalyst_inputs(s, "MU")
    earn_fetch = next(f for f in fetches if f.source == "earnings")
    assert earn_fetch.last_status == "degraded"
    assert earn_fetch.last_success_at == _NOW


@pytest.mark.parametrize(
    "error", [UnusualWhalesDailyLimitError("daily_request_limit"), UnusualWhalesAuthError("401")],
)
async def test_daily_limit_and_auth_propagate(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient(errors={_earn("MU"): error})
    with pytest.raises(type(error)):
        await _refresh_all(sessions, settings, "MU", client)
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


async def test_every_generated_chip_string_is_clean(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient({
        _earn("IONS"): {"data": [
            {"source": "estimation", "report_time": "premarket", "report_date": "2026-09-21"},
        ]},
        cat.FDA_PATH: _FDA_IONS,
        cat.ECONOMIC_CALENDAR_PATH: _ECON,
    })
    await _refresh_all(sessions, settings, "IONS", client)
    texts = [
        cat.CHIP_HEAD, cat.NONE_FOUND, cat.UNKNOWN, cat.VAGUE_TIMING, cat.ESTIMATED_MARK,
        cat.BEYOND_HORIZON_TEMPLATE, cat.M22_MAY_DIFFER, *cat.KIND_LABELS.values(),
        *cat.EARNINGS_TIMING_LABELS.values(),
    ]
    for ticker in ("IONS", "NOPE"):
        for days in (1, 3, 7, 10, 30, 400):
            for start in (_NOW, datetime(2026, 9, 20, tzinfo=UTC)):
                chip = _chip(sessions, settings, ticker, start + timedelta(days=days), start=start)
                texts.append(chip.text)
                texts.extend(p.text for p in chip.parts)
    for text in texts:
        assert ensure_clean(text) == text
