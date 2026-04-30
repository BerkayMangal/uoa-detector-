"""Pipeline orchestrator. Wires source → stages → scoring → penalties → labeler
→ sizer → backtest store, emitting one structured log line per event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize
from uoa_detector.labeling.labeler import Labeler
from uoa_detector.pipeline.stage import EnrichmentStage, PipelineContext
from uoa_detector.risk.sizer import RiskSizer
from uoa_detector.scoring.combined import compute_combined_score
from uoa_detector.scoring.penalties import PenaltyEngine

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.sources.base import FlowDataSource

_logger = structlog.get_logger(__name__)


class PipelineResult(BaseModel):
    """One fully-processed event: the enriched state plus its label and size."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event: EnrichedEvent
    decision: LabelDecision
    size: PositionSize


class Pipeline:
    """End-to-end runner. One ``Pipeline`` instance per process / scenario."""

    def __init__(
        self,
        source: FlowDataSource,
        stages: Sequence[EnrichmentStage],
        *,
        profile: CalibrationProfile | None = None,
        store: BacktestStore | None = None,
        context: PipelineContext | None = None,
    ) -> None:
        self._source = source
        self._stages = list(stages)
        self._profile = profile or load_default_profile()
        self._store = store or BacktestStore()
        self._ctx = context or PipelineContext(profile=self._profile)
        self._penalty_engine = PenaltyEngine(self._profile)
        self._labeler = Labeler(self._profile)
        self._sizer = RiskSizer(self._profile)

    @property
    def store(self) -> BacktestStore:
        """The backtest store this pipeline writes to."""
        return self._store

    @property
    def context(self) -> PipelineContext:
        """The shared ``PipelineContext`` (exposed mainly for tests)."""
        return self._ctx

    async def run(self) -> list[PipelineResult]:
        """Drain the source, process each event, return all results."""
        results: list[PipelineResult] = []
        try:
            async for raw in self._source.stream():
                results.append(await self.process_one(raw))
        finally:
            await self._source.close()
        return results

    async def process_one(self, raw: OptionsPrint) -> PipelineResult:
        """Run one print through the full pipeline.

        If any enrichment stage sets ``event.rejection`` (e.g., Module 39 under
        the ``reject`` extended-hours policy), the orchestrator short-circuits:
        no further enrichment, no penalty engine, no scoring, no labeler, no
        sizer. The event is recorded in the backtest store with a sentinel
        ``IGNORE_NOISE`` label whose ``reason`` carries the rejection details
        and a zero-R ``PositionSize``. The full ``RejectedEvent`` is preserved
        on ``event.rejection`` for the decision record / audit.
        """
        event = EnrichedEvent(print=raw)

        # 1) Enrichment stages — break early on rejection.
        for stage in self._stages:
            event = await stage.enrich(event, self._ctx)
            if event.rejection is not None:
                break

        if event.rejection is not None:
            return self._handle_rejection(event)

        # 2) Penalty engine (must run BEFORE scoring so combined-score-post is correct)
        self._penalty_engine.apply(event)

        # 3) Scoring engine
        compute_combined_score(event, self._profile)

        # 4) Labeler
        decision = self._labeler.decide(event)

        # 5) Risk sizer
        size = self._sizer.size_for(decision.label)

        # 6) Persist
        self._store.add(event, decision, size)

        # 7) Push event into the rolling buffer for the next event's clustering
        self._ctx.recent_events.append(event)

        # 8) One structured log line per event
        _logger.info(
            "signal",
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

        return PipelineResult(event=event, decision=decision, size=size)

    def _handle_rejection(self, event: EnrichedEvent) -> PipelineResult:
        """Short-circuit path for an event whose ``rejection`` field is set.

        Skips penalty/scoring/labeling/sizing. Records a sentinel decision so
        the backtest store still has one row per print; the rejection record
        on ``event.rejection`` carries the full reason for audit.

        Note: rejected events do NOT enter ``ctx.recent_events`` — they should
        not influence Module 38 cluster counting since they were never scored.
        """
        assert event.rejection is not None  # invariant: caller checked
        rejection = event.rejection
        decision = LabelDecision(
            label=SignalLabel.IGNORE_NOISE,
            reason=f"Rejected by {rejection.rejected_by_stage}: {rejection.reason}",
        )
        size = self._sizer.size_for(decision.label)
        self._store.add(event, decision, size)

        _logger.info(
            "signal_rejected",
            ts=event.print_.timestamp.isoformat(),
            ticker=event.print_.ticker,
            stage=rejection.rejected_by_stage,
            reason=rejection.reason,
        )

        return PipelineResult(event=event, decision=decision, size=size)
