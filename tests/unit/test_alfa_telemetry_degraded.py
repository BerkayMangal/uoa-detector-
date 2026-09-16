"""Phase 5.2.A0c: degraded-provider detection in the Alfa Board telemetry writer.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A3 ("Degraded
calls") and §9 (``deg`` always yields ``bilinmiyor``).

A ``Degrading*`` wrapper maps a transient UW error to no-data, so the stage's
branch string reads like real missing data. The writer tells them apart by
diffing each wrapper's public ``errors`` counter between consecutive records.

Pins:
  - wrappers are discovered on the stages of
    ``build_live_stage_pipeline(..., degrade_transient_errors=True)``:
    M21 dealer gamma; M22 catalyst; M24 IV history and the same catalyst
    instance; M25 sector map and peer flow; M26 dark pool; M27 open interest;
  - the builder default (no degradation) yields no wrappers;
  - a stage is degraded only for the event during which its wrapper counted a
    new error; errors counted before the writer existed do not count;
  - a shared wrapper (the M22/M24 catalyst) marks every stage holding it;
  - through a real ``Pipeline`` on a client failing with HTTP 503, each wrapped
    stage's flag equals "its wrappers counted an error", M21 is degraded, and
    unwrapped stages (M23 included) are not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from webapp.board.telemetry import (
    AlfaStageTelemetry,
    AlfaTelemetryWriter,
    degrading_wrappers_by_stage,
)

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    StageExecutionEntry,
)
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import build_live_stage_pipeline
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print
from uoa_detector.sources.unusual_whales.client import UnusualWhalesTransientError
from uoa_detector.sources.unusual_whales.providers.degrading import (
    DegradingCatalystCalendarProvider,
    DegradingDarkPoolPrintProvider,
    DegradingDealerPositioningProvider,
    DegradingIVHistoryProvider,
    DegradingOpenInterestProvider,
    DegradingPeerFlowProvider,
    DegradingSectorMapProvider,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN = "live-2026-09-14"
_TS = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_WRAPPED_STAGES = {
    "m21_dealer_gamma", "m22_event_calendar", "m24_iv_exhaustion",
    "m25_sector_peer", "m26_dark_pool", "m27_opening_closing",
}


class _Http503Client:
    """Every request fails as the client does after exhausting 5xx retries."""

    def __init__(self) -> None:
        self.calls = 0

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params
        self.calls += 1
        msg = f"UnusualWhales {method} {path} failed after 3 attempts (HTTP 503)"
        raise UnusualWhalesTransientError(msg)


class _Counter:
    def __init__(self, errors: int = 0) -> None:
        self.errors = errors


def _live_stages() -> list[Any]:
    return build_live_stage_pipeline(
        _Http503Client(), load_default_profile(), degrade_transient_errors=True,  # type: ignore[arg-type]
    )


def _record(event_id: str, stage_names: list[str]) -> SignalDecisionRecord:
    return SignalDecisionRecord(
        decision_emitted_at=_TS,
        profile_id="v5_default",
        profile_content_hash="hash",
        event=EnrichedEvent(print=build_print(event_id=event_id, ts=_TS, ticker="SPY")),
        stage_executions=[StageExecutionEntry(stage_name=n, latency_ms=0.0) for n in stage_names],
        score_breakdown={},
        decision=LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        size=PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _degraded_by_event(url: str) -> dict[str, dict[str, bool]]:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            rows = list(session.scalars(select(AlfaStageTelemetry)))
    finally:
        engine.dispose()
    out: dict[str, dict[str, bool]] = {}
    for row in rows:
        out.setdefault(row.event_id, {})[row.stage_name] = row.degraded
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_wrappers_are_discovered_per_stage_on_the_live_builder() -> None:
    stages = _live_stages()
    by_type = {type(s): s for s in stages}
    found = degrading_wrappers_by_stage(stages)

    assert set(found) == _WRAPPED_STAGES

    (m21,) = found["m21_dealer_gamma"]
    assert m21 is by_type[DealerGammaStage]._provider
    assert isinstance(m21, DegradingDealerPositioningProvider)

    (m22,) = found["m22_event_calendar"]
    assert m22 is by_type[EventCalendarStage]._provider
    assert isinstance(m22, DegradingCatalystCalendarProvider)

    m24 = found["m24_iv_exhaustion"]
    assert len(m24) == 2
    m24_stage = by_type[IVExhaustionStage]
    assert any(w is m24_stage._iv_provider for w in m24)
    assert any(w is m22 for w in m24)  # the one shared catalyst wrapper
    assert any(isinstance(w, DegradingIVHistoryProvider) for w in m24)

    m25 = found["m25_sector_peer"]
    m25_stage = by_type[SectorPeerStage]
    assert len(m25) == 2
    assert any(w is m25_stage._sector_provider for w in m25)
    assert any(w is m25_stage._peer_flow_provider for w in m25)
    assert {type(w) for w in m25} == {DegradingSectorMapProvider, DegradingPeerFlowProvider}

    (m26,) = found["m26_dark_pool"]
    assert m26 is by_type[DarkPoolStage]._provider
    assert isinstance(m26, DegradingDarkPoolPrintProvider)

    (m27,) = found["m27_opening_closing"]
    assert m27 is by_type[OpeningClosingStage]._provider
    assert isinstance(m27, DegradingOpenInterestProvider)


def test_builder_default_has_no_wrappers() -> None:
    stages = build_live_stage_pipeline(_Http503Client(), load_default_profile())  # type: ignore[arg-type]
    assert degrading_wrappers_by_stage(stages) == {}


def test_objects_without_a_name_are_ignored() -> None:
    assert degrading_wrappers_by_stage([object(), 42]) == {}


# ---------------------------------------------------------------------------
# Counter diff
# ---------------------------------------------------------------------------


def test_counter_diff_marks_only_stages_whose_wrapper_failed_on_this_event(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'alfa.db'}"
    dealer = _Counter()
    catalyst = _Counter()
    iv = _Counter(errors=5)  # errors before the writer existed do not count
    dark_pool = _Counter()
    writer = AlfaTelemetryWriter(
        database_url=url,
        run_id_source=lambda: _RUN,
        degrading_by_stage={
            "m21_dealer_gamma": (dealer,),
            "m22_event_calendar": (catalyst,),
            "m24_iv_exhaustion": (iv, catalyst),
            "m26_dark_pool": (dark_pool,),
        },
    )
    names = [
        "m21_dealer_gamma", "m22_event_calendar", "m23_price_confirmation",
        "m24_iv_exhaustion", "m26_dark_pool",
    ]

    dealer.errors += 1
    writer.write(_record("e1", names))
    writer.write(_record("e2", names))  # nothing new
    catalyst.errors += 2
    writer.write(_record("e3", names))
    iv.errors += 1
    dark_pool.errors += 1
    writer.write(_record("e4", names))
    writer.close()

    flags = _degraded_by_event(url)
    assert flags["e1"] == {
        "m21_dealer_gamma": True, "m22_event_calendar": False,
        "m23_price_confirmation": False, "m24_iv_exhaustion": False, "m26_dark_pool": False,
    }
    assert not any(flags["e2"].values())
    assert flags["e3"] == {
        "m21_dealer_gamma": False, "m22_event_calendar": True,
        "m23_price_confirmation": False, "m24_iv_exhaustion": True, "m26_dark_pool": False,
    }
    assert flags["e4"] == {
        "m21_dealer_gamma": False, "m22_event_calendar": False,
        "m23_price_confirmation": False, "m24_iv_exhaustion": True, "m26_dark_pool": True,
    }


def test_counter_diff_advances_even_when_the_database_write_fails(tmp_path: Path) -> None:
    good_url = f"sqlite:///{tmp_path / 'alfa.db'}"
    dealer = _Counter()
    broken = AlfaTelemetryWriter(
        database_url=f"sqlite:///{tmp_path / 'missing' / 'x.db'}",
        run_id_source=lambda: _RUN,
        degrading_by_stage={"m21_dealer_gamma": (dealer,)},
    )
    dealer.errors += 1
    broken.write(_record("lost", ["m21_dealer_gamma"]))
    # Same tracker state, now pointed at a working database.
    broken._database_url = good_url
    broken.write(_record("next", ["m21_dealer_gamma"]))
    broken.close()

    assert _degraded_by_event(good_url) == {"next": {"m21_dealer_gamma": False}}


# ---------------------------------------------------------------------------
# Through a real Pipeline
# ---------------------------------------------------------------------------


async def test_pipeline_transient_errors_mark_wrapped_stages_degraded(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'alfa.db'}"
    profile = load_default_profile()
    stages = _live_stages()
    found = degrading_wrappers_by_stage(stages)
    store = SqliteBacktestStore(url, flush_threshold=1, replay_safe=True)
    try:
        store.start_run(profile=profile, universe_id="live", run_id=_RUN)
        writer = AlfaTelemetryWriter.for_stages(
            database_url=url, run_id_source=lambda: store.active_run_id, stages=stages,
        )
        raw = to_raw_print(
            build_print(event_id="live-503", ts=_TS, ticker="SPY", strike="600", spot="598"),
            source_id="unusual_whales",
        )
        await Pipeline(
            [SyntheticRawFlowSource("unusual_whales", [raw])], stages,
            profile=profile, store=store, decision_record_writer=writer,
        ).run()
    finally:
        store.close()

    flags = _degraded_by_event(url)["live-503"]
    assert set(flags) == {s.name for s in stages}
    for name, wrappers in found.items():
        assert flags[name] is (sum(w.errors for w in wrappers) > 0), name
    assert flags["m21_dealer_gamma"] is True
    for name in set(flags) - _WRAPPED_STAGES:
        assert flags[name] is False, name
