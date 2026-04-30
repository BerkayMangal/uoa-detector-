"""``RejectedEvent`` — marker for events dropped by an enrichment stage.

Rejection is a structurally-distinct outcome from ``IGNORE_NOISE``: the latter
is "evaluated and found unremarkable", the former is "deliberately not
evaluated" (e.g., extended-hours print under the ``reject`` policy). Stages
signal rejection by setting ``EnrichedEvent.rejection``; the pipeline
orchestrator detects this and short-circuits the remaining stages, scoring,
penalties, labeling, and sizing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RejectedEvent(BaseModel):
    """Structured rejection record, attached to ``EnrichedEvent.rejection``.

    Treated as terminal: once set, downstream pipeline stages and the scoring
    engine MUST NOT run on the event. The decision record carries this verbatim
    for audit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    rejected_by_stage: str
    rejected_at: datetime  # tz-aware UTC; the stage's PipelineContext.now()
