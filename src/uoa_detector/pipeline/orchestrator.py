"""Pipeline orchestrator. Wires sources → fusion → stages → scoring → penalties
→ labeler → sizer → backtest store, emitting one structured log line per event.

Phase 2.3.4: ``Pipeline`` consumes one or more ``RawFlowSource``s and runs
them through ``SourceFusion`` internally. Single-source scenarios get
``confidence_tier='single'`` on every event with no windowing wait;
multi-source scenarios get watermark-driven fusion per ``profile.fusion``.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision
from uoa_detector.domain.risk import PositionSize
from uoa_detector.fusion import SourceFusion
from uoa_detector.labeling.labeler import Labeler
from uoa_detector.observability import (
    DecisionRecordWriter,
    SignalDecisionRecord,
    StageExecutionEntry,
    build_decision_record,
)
from uoa_detector.pipeline.cluster_decay import ClusterDecayWatcher
from uoa_detector.pipeline.stage import EnrichmentStage, PipelineContext
from uoa_detector.risk.sizer import RiskSizer
from uoa_detector.scoring.combined import compute_combined_score
from uoa_detector.scoring.penalties import PenaltyEngine

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.sources.base import RawFlowSource

_logger = structlog.get_logger(__name__)


class PipelineResult(BaseModel):
    """One fully-processed event: the enriched state plus its label and size."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event: EnrichedEvent
    decision: LabelDecision
    size: PositionSize
    # Phase 2.7: every result carries a SignalDecisionRecord so observers can
    # serialize, log, or persist as they choose. The record is populated by
    # process_one regardless of whether a writer is attached.
    record: SignalDecisionRecord | None = None


class Pipeline:
    """End-to-end runner. One ``Pipeline`` instance per process / scenario.

    Takes one or more ``RawFlowSource``s and constructs a ``SourceFusion``
    internally using ``profile.fusion`` settings. Single source → fast path
    (tier='single'); multiple sources → windowed fusion.
    """

    def __init__(
        self,
        sources: Sequence[RawFlowSource],
        stages: Sequence[EnrichmentStage],
        *,
        profile: CalibrationProfile | None = None,
        store: BacktestStore | None = None,
        context: PipelineContext | None = None,
        force_multi_source: bool = False,
        decay_watcher_enabled: bool = True,
        decay_check_interval_s: float = 60.0,
        decision_record_writer: DecisionRecordWriter | None = None,
    ) -> None:
        self._profile = profile or load_default_profile()
        self._fusion = SourceFusion(
            sources,
            self._profile.fusion,
            force_multi_source=force_multi_source,
        )
        self._stages = list(stages)
        self._store = store or BacktestStore()
        self._ctx = context or PipelineContext(profile=self._profile)
        self._penalty_engine = PenaltyEngine(self._profile)
        self._labeler = Labeler(self._profile)
        self._sizer = RiskSizer(self._profile)
        self._decay_watcher_enabled = decay_watcher_enabled
        self._decay_check_interval_s = decay_check_interval_s
        self._writer = decision_record_writer

    @property
    def store(self) -> BacktestStore:
        """The backtest store this pipeline writes to."""
        return self._store

    @property
    def context(self) -> PipelineContext:
        """The shared ``PipelineContext`` (exposed mainly for tests)."""
        return self._ctx

    async def run(self) -> list[PipelineResult]:
        """Drain the fused source stream, process each event, return all results.

        If ``decay_watcher_enabled`` (the default), a background
        ``ClusterDecayWatcher`` task runs alongside event processing,
        flipping ``cluster_decayed=True`` on stale cluster buffers every
        ``decay_check_interval_s`` walltime. Cancelled cleanly on close.
        """
        watcher_task: asyncio.Task[None] | None = None
        watcher: ClusterDecayWatcher | None = None
        if self._decay_watcher_enabled:
            watcher = ClusterDecayWatcher(
                self._ctx,
                check_interval_s=self._decay_check_interval_s,
            )
            watcher_task = asyncio.create_task(watcher.run())

        results: list[PipelineResult] = []
        try:
            async for canonical in self._fusion.stream():
                results.append(await self.process_one(canonical))
        finally:
            if watcher is not None and watcher_task is not None:
                watcher.stop()
                try:
                    await asyncio.wait_for(watcher_task, timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    watcher_task.cancel()
            await self._fusion.close()
            if self._writer is not None:
                self._writer.close()
        return results

    async def process_one(self, raw: OptionsPrint) -> PipelineResult:
        """Run one print through the full pipeline.

        If any enrichment stage sets ``event.rejection`` (e.g., Module 39 under
        the ``reject`` extended-hours policy), the orchestrator skips remaining
        enrichment, the penalty engine, and the scoring engine — but still
        calls the labeler, which returns ``REJECTED`` at its precedence-step-0
        gate. The risk sizer then maps ``REJECTED`` to its dedicated bucket
        with zero R. The full ``RejectedEvent`` is preserved on
        ``event.rejection`` for the decision record / audit.
        """
        event = EnrichedEvent(print=raw)
        stage_executions: list[StageExecutionEntry] = []

        # 1) Enrichment stages — break early on rejection.
        # Per-stage walltime captured for the decision record.
        for stage in self._stages:
            t0 = time.perf_counter()
            event = await stage.enrich(event, self._ctx)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            stage_executions.append(
                StageExecutionEntry(
                    stage_name=stage.name,
                    latency_ms=latency_ms,
                ),
            )
            if event.rejection is not None:
                break

        rejected = event.rejection is not None

        if not rejected:
            # 2) Penalty engine (must run BEFORE scoring so combined-score-post is correct)
            self._penalty_engine.apply(event)
            # 3) Scoring engine
            compute_combined_score(event, self._profile)

        # 4) Labeler — handles REJECTED at step 0 if event.rejection is set.
        decision = self._labeler.decide(event)

        # 5) Risk sizer
        size = self._sizer.size_for(decision.label)

        # 6) Persist
        self._store.add(event, decision, size)

        # 7) Rolling-buffer membership: rejected events do NOT influence
        # Module 38 cluster counting since they were never scored.
        if not rejected:
            self._ctx.recent_events.append(event)

        # 8) One structured log line per event
        _logger.info(
            "signal_rejected" if rejected else "signal",
            ts=raw.timestamp.isoformat(),
            ticker=raw.ticker,
            option_type=raw.option_type,
            strike=str(raw.strike),
            dte=raw.dte,
            label=decision.label.value,
            combined_score=(
                round(event.combined_score_post_penalty, 4)
                if event.combined_score_post_penalty is not None
                else None
            ),
            max_r=size.max_r,
            reason=decision.reason,
        )

        # 9) Build decision record + dispatch to writer if attached.
        record = build_decision_record(
            event=event,
            profile=self._profile,
            decision=decision,
            size=size,
            stage_executions=stage_executions,
        )
        if self._writer is not None:
            self._writer.write(record)

        return PipelineResult(
            event=event,
            decision=decision,
            size=size,
            record=record,
        )

        return PipelineResult(event=event, decision=decision, size=size)
