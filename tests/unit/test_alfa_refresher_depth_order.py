"""Phase 5.2.A4: the refresher's top-K exit-depth calls follow the board's order.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.4 (exit depth for the
top-K rows) and §5 A4 (row order: L desc, A asc, U asc, total premium desc).

Pins:
  - with K = 1 the depth call goes to the row with more lehte families, not to
    the row with the larger premium;
  - legacy scores (the live calibration profile) count toward that order;
  - when the evidence tables cannot be read, the order falls back to total
    premium and a warning is logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import webapp.board.refresher as refresher_module
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.board.evidence import STAGE_BY_FAMILY, LegacyScores, load_legacy_scores
from webapp.board.quotes import ensure_quotes_tables
from webapp.board.refresher import SoftCap, run_quotes_cycle
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry, ensure_telemetry_tables

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TOP_1 = _SETTINGS.model_copy(update={"refresh": _SETTINGS.refresh.model_copy(update={"exit_depth_top_k": 1})})
_LEGACY = load_legacy_scores(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_RUN = "live-2026-09-15"
_SPY = "SPY260918C00760000"
_SMCI = "SMCI260918C00037000"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last_daily_request_count: int | None = None
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append(path)
        return {"data": []}


class _Journal:
    def list(self, status: str | None = None) -> list[Any]:
        return []


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


def _seed(url: str, *, smci_telemetry: bool, smci_price_score: float | None = None) -> None:
    specs = [("e1", "SPY", "760", "600000", True), ("e2", "SMCI", "37", "200000", smci_telemetry)]
    store = SqliteBacktestStore(url, flush_threshold=len(specs) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (eid, ticker, strike, premium, _meta) in enumerate(specs):
            pr = build_print(
                event_id=eid, ts=_NOW - timedelta(minutes=10, seconds=-i), ticker=ticker,
                option_type="call", strike=strike, dte=3, premium=premium,
            )
            score = smci_price_score if ticker == "SMCI" else None
            store.add(
                EnrichedEvent(print=pr, price_confirmation_score=score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            for eid, ticker, strike, _premium, with_meta in specs:
                if not with_meta:
                    continue
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=eid, ticker=ticker, option_chain=None, fill_side="at_ask",
                        option_type="call", strike=strike, expiry=(_NOW + timedelta(days=3)).date(),
                        print_ts=_NOW, written_at=_NOW,
                    ),
                )
            if smci_telemetry:
                session.add_all(
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id="e2", stage_name=STAGE_BY_FAMILY[family], branch=branch,
                        metadata_json=None, degraded=False, profile_content_hash="h", written_at=_NOW,
                    )
                    for family, branch in (
                        ("dealer_gamma", "full_short_and_proximate"),
                        ("dark_pool", "confirmed_match"),
                        ("sector", "strong"),
                    )
                )
            session.commit()
    finally:
        engine.dispose()


async def _depth_calls(url: str, *, legacy_scores: LegacyScores | None = None) -> list[str]:
    client = _FakeClient()
    engine = make_engine(url)
    try:
        ensure_quotes_tables(engine)
        await run_quotes_cycle(
            client, engine, _TOP_1,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine), journal=_Journal(),
            soft_cap=SoftCap(_SETTINGS.refresh.daily_request_soft_cap), clock=lambda: _NOW,
            legacy_scores=legacy_scores,
        )
    finally:
        engine.dispose()
    return [path.split("/")[3] for path in client.calls if path.endswith("/flow")]


async def test_top_k_depth_follows_the_board_evidence_order(url: str) -> None:
    _seed(url, smci_telemetry=True)
    assert await _depth_calls(url) == [_SMCI]


async def test_legacy_scores_count_toward_the_depth_order(url: str) -> None:
    _seed(url, smci_telemetry=False, smci_price_score=1.0)
    assert await _depth_calls(url, legacy_scores=_LEGACY) == [_SMCI]
    assert await _depth_calls(url) == [_SPY]  # without legacy scores nothing maps: premium order


async def test_unreadable_evidence_keeps_total_premium_order(
    url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url, smci_telemetry=True)

    def _broken(engine: object, run_id: str, requests: object) -> Any:
        msg = "evidence tables unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(refresher_module, "read_evidence_inputs", _broken)
    with caplog.at_level(logging.WARNING, logger="webapp.board.refresher"):
        assert await _depth_calls(url) == [_SPY]
    assert "evidence tables unreadable" in caplog.text
