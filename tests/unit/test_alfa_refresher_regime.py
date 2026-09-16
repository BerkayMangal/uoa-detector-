"""Phase 5.2.B5b: the refresher's regime step (``webapp/board/refresher.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher"),
§4.4 (regime: seven calls per 300 s cycle, ≈550 a day) and §6 B5.

The payloads are the trimmed live responses of ``test_board_regime``.

Pins:
  - one cycle makes exactly the seven contract calls, with their parameters,
    and the VIX futures endpoint is never called;
  - what it writes is what the render path reads back;
  - the soft cap pauses the whole step;
  - daily-limit and key failures propagate; another 4xx degrades the step;
  - inside the loop the step closes the cycle and its failure is isolated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import webapp.board.refresher as refresher_module
from webapp.board.atm import ensure_atm_tables
from webapp.board.db import make_engine
from webapp.board.netprem import ensure_netprem_tables
from webapp.board.quotes import ensure_quotes_tables
from webapp.board.refresher import SoftCap, run_regime_cycle
from webapp.board.regime import ensure_regime_tables, read_regime_inputs
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.ticker_info import ensure_ticker_info_tables

from tests.conftest import build_print
from tests.unit.test_board_regime import _paths
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CAP = _SETTINGS.refresh.daily_request_soft_cap
_NOW = datetime(2026, 9, 15, 15, 28, 30, tzinfo=UTC)  # the fetch time of the trimmed payloads
_CALLS = [
    ("/api/market/market-tide", {"interval_5m": "true"}),
    ("/api/stock/SPY/spot-exposures", None),
    ("/api/stock/QQQ/spot-exposures", None),
    ("/api/stock/SPY/gex-levels", {"source": "oi"}),
    ("/api/stock/QQQ/gex-levels", {"source": "oi"}),
    ("/api/stock/SPY/volatility/term-structure", None),
    ("/api/stock/VIX/volatility/term-structure", None),
]


class _Client:
    def __init__(
        self, *, errors: dict[str, BaseException] | None = None, count: int | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.last_daily_request_count = count
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)
        self._responses = _paths()
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        for fragment, error in self._errors.items():
            if fragment in path:
                raise error
        return self._responses.get(path, {"data": []})

    async def aclose(self) -> None:
        return None


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'regime.db'}")
    ensure_regime_tables(engine)
    return engine


async def _cycle(engine: Engine, client: _Client, *, soft_cap: SoftCap | None = None) -> Any:
    return await run_regime_cycle(
        client, engine, _SETTINGS,  # type: ignore[arg-type]
        soft_cap=soft_cap or SoftCap(_CAP), clock=lambda: _NOW,
    )


async def test_one_cycle_makes_the_seven_contract_calls(engine: Engine) -> None:
    client = _Client()
    try:
        report = await _cycle(engine, client)
    finally:
        engine.dispose()
    assert client.calls == _CALLS
    assert report.requests == 7
    assert report.skipped_non_critical is False
    assert not any("vix-term-structure" in path for path, _params in client.calls)


async def test_what_the_cycle_writes_is_what_the_page_reads(engine: Engine) -> None:
    try:
        await _cycle(engine, _Client())
        inputs = read_regime_inputs(engine, now=_NOW + timedelta(seconds=30))
    finally:
        engine.dispose()
    assert inputs.tide_buckets  # the last complete 5-minute bucket
    assert {reading.ticker for reading in inputs.gamma} == {"SPY", "QQQ"}
    assert {levels.ticker for levels in inputs.gex} == {"SPY", "QQQ"}
    assert inputs.curve is not None and inputs.vix is not None


async def test_the_soft_cap_pauses_the_whole_step(engine: Engine) -> None:
    reached = SoftCap(_CAP)
    reached.observe(_CAP, _NOW)
    client = _Client()
    try:
        report = await _cycle(engine, client, soft_cap=reached)
    finally:
        engine.dispose()
    assert (client.calls, report.requests, report.skipped_non_critical) == ([], 0, True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_daily_limit_and_key_failures_propagate(engine: Engine, error: Exception) -> None:
    try:
        with pytest.raises(type(error)):
            await _cycle(engine, _Client(errors={"market-tide": error}))
    finally:
        engine.dispose()


async def test_another_4xx_degrades_the_step(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    bad = UnusualWhalesAuthError("UnusualWhales GET /api/market/market-tide returned HTTP 400: bad")
    try:
        with caplog.at_level(logging.WARNING, logger="webapp.board.refresher"):
            report = await _cycle(engine, _Client(errors={"market-tide": bad}))
    finally:
        engine.dispose()
    assert (report.requests, report.degraded_fetches, report.skipped_non_critical) == (0, 1, False)
    assert "marked degraded" in caplog.text


async def test_the_soft_cap_observes_the_header_even_after_a_degraded_step(engine: Engine) -> None:
    bad = UnusualWhalesAuthError("UnusualWhales GET /api/market/market-tide returned HTTP 400: bad")
    soft_cap = SoftCap(_CAP)
    try:
        await _cycle(engine, _Client(errors={"market-tide": bad}, count=_CAP), soft_cap=soft_cap)
    finally:
        engine.dispose()
    assert soft_cap.count == _CAP
    assert soft_cap.reached(_NOW) is True


def _seed_run(url: str) -> None:
    """One live print, so the market cycle has a board to refresh."""
    store = SqliteBacktestStore(url, flush_threshold=2)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id="live-2026-09-15")
        store.add(
            EnrichedEvent(
                print=build_print(
                    event_id="e1", ts=_NOW - timedelta(minutes=30), ticker="SPY",
                    option_type="call", strike="757", dte=3, premium="500000",
                ),
            ),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
    finally:
        store.close()


async def test_a_failing_regime_step_is_isolated_from_the_rest_of_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    url = f"sqlite:///{tmp_path / 'cycle.db'}"
    _seed_run(url)
    engine = make_engine(url)
    for ensure in (ensure_quotes_tables, ensure_netprem_tables, ensure_ticker_info_tables,
                   ensure_atm_tables, ensure_regime_tables):
        ensure(engine)

    async def _broken(*args: object, **kwargs: object) -> object:
        msg = "alfa_regime unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(refresher_module, "run_regime_cycle", _broken)
    client = _Client()
    try:
        with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"):
            reports = await refresher_module._run_market_cycle(
                client, engine, _SETTINGS,  # type: ignore[arg-type]
                reader=BoardSignalReader(engine=engine),
                journal=SimpleNamespace(list=lambda status=None: []),  # type: ignore[arg-type]
                soft_cap=SoftCap(_CAP), clock=lambda: _NOW, legacy_scores=None,
            )
    finally:
        engine.dispose()
    assert reports.regime is None  # the step failed and was isolated
    assert reports.tape is not None and reports.info is not None  # the earlier steps still ran
    assert "board refresher cycle failed at step regime" in caplog.text
    assert any(path.endswith("/net-prem-ticks") for path, _params in client.calls)
