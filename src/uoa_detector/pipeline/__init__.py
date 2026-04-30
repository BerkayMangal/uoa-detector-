"""Pipeline orchestration: stage Protocol, runner, and shared context."""

from uoa_detector.pipeline.orchestrator import Pipeline, PipelineResult
from uoa_detector.pipeline.stage import EnrichmentStage, PipelineContext

__all__ = ["EnrichmentStage", "Pipeline", "PipelineContext", "PipelineResult"]
