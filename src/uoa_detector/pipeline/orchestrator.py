"""Pipeline orchestrator. Wires sources → fusion → stages → scoring → penalties
→ labeler → sizer → backtest store, emitting one structured log line per event.

Phase 2.3.4: ``Pipeline`` consumes one or more ``RawFlowSource``s and runs
them through ``SourceFusion`` internally. Single-source scenarios get
``confidence_tier='single'`` on every event with no windowing wait;
multi-source scenarios get watermark-driven fusion per ``profile.fusion``.
"""

from __future__ import annotations

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

    @property
    def store(self) -> BacktestStore:
        """The backtest store this pipeline writes to."""
        return self._store

    @property
    def context(self) -> PipelineContext:
        """The shared ``PipelineContext`` (exposed mainly for tests)."""
        return self._ctx

    async def run(self) -> list[PipelineResult]:
        """Drain the fused source stream, process each event, return all results."""
        results: list[PipelineResult] = []
        try:
            async for canonical in self._fusion.stream():
                results.append(await self.process_one(canonical))
        finally:
            await self._fusion.close()
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

        # 1) Enrichment stages — break early on rejection.
        for stage in self._stages:
            event = await stage.enrich(event, self._ctx)
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

        return PipelineResult(event=event, decision=decision, size=size)
