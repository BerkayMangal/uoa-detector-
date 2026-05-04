"""Pipeline orchestrator. Wires sources → fusion → stages → scoring → penalties
→ labeler → sizer → backtest store, emitting one structured log line per event.

Phase 2.3.4: ``Pipeline`` consumes one or more ``RawFlowSource``s and runs
them through ``SourceFusion`` internally. Single-source scenarios get
``confidence_tier='single'`` on every event with no windowing wait;
multi-source scenarios get watermark-driven fusion per ``profile.fusion``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.calibration.resolver import CalibrationResolver
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
        resolver: CalibrationResolver | None = None,
        store: BacktestStore | None = None,
        context: PipelineContext | None = None,
        force_multi_source: bool = False,
        decision_record_writer: DecisionRecordWriter | None = None,
    ) -> None:
        # Profile resolution: if resolver is supplied, we snapshot
        # resolver.default() at the start of each process_one call so
        # mid-run profile reloads are picked up at event boundaries (never
        # mid-event — the in-flight event finishes on its captured snapshot).
        # If resolver is None (the back-compat path), engines are built from
        # the static profile passed at construction; no hot-swap.
        self._resolver = resolver
        if resolver is not None and profile is None:
            self._profile = resolver.default()
        else:
            self._profile = profile or load_default_profile()
        self._fusion = SourceFusion(
            sources,
            self._profile.fusion,
            force_multi_source=force_multi_source,
        )
        self._stages = list(stages)
        self._store = store or BacktestStore()
        self._ctx = context or PipelineContext(profile=self._profile)
        # Engines built from the init-time profile. When resolver is provided,
        # process_one rebuilds these per event from the snapshot (engines
        # don't cache derived state, so reconstruction is cheap).
        self._penalty_engine = PenaltyEngine(self._profile)
        self._labeler = Labeler(self._profile)
        self._sizer = RiskSizer(self._profile)
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

        Cluster decay runs as a pipeline stage (``ClusterDecayStage``, added
        to the default pipeline in Phase 3.1.1) — no background task, no
        walltime dependency. Each event triggers one decay-check pass using
        its own timestamp as the clock. Backtest-replay correct.
        """
        results: list[PipelineResult] = []
        try:
            async for canonical in self._fusion.stream():
                results.append(await self.process_one(canonical))
        finally:
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

        Profile snapshot semantics: when the pipeline was constructed with a
        ``CalibrationResolver``, this method captures ``resolver.default()``
        at entry and uses that snapshot for the whole event — engines
        (penalty / labeler / sizer) are rebuilt from the snapshot, scoring
        reads the snapshot, and the decision record carries that snapshot's
        ``profile_id`` and ``content_hash``. A concurrent ``resolver.reload()``
        won't affect this in-flight event; the next ``process_one`` call sees
        the new profile. When constructed without a resolver, the static
        init-time profile is used (no per-event snapshot, no hot-swap).
        """
        # Snapshot the active profile for this event. Do this once at entry
        # so engine reconstruction and the decision record use the same
        # CalibrationProfile object — avoids torn state if the resolver
        # reloads between this call and engine setup.
        if self._resolver is not None:
            event_profile = self._resolver.default()
            penalty_engine = PenaltyEngine(event_profile)
            labeler = Labeler(event_profile)
            sizer = RiskSizer(event_profile)
            # Update the shared context's profile so stages see the snapshot.
            # (Stages read ctx.profile, not self._profile.)
            self._ctx.profile = event_profile
        else:
            event_profile = self._profile
            penalty_engine = self._penalty_engine
            labeler = self._labeler
            sizer = self._sizer

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
            penalty_engine.apply(event)
            # 3) Scoring engine
            compute_combined_score(event, event_profile)

        # 4) Labeler — handles REJECTED at step 0 if event.rejection is set.
        decision = labeler.decide(event)

        # 5) Risk sizer
        size = sizer.size_for(decision.label)

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
            profile=event_profile,
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
