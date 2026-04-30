"""Typed exceptions for the UOA detector. Do not catch broad ``Exception`` — use these."""

from __future__ import annotations


class UOADetectorError(Exception):
    """Base class for all UOA detector errors."""


class ConfigurationError(UOADetectorError):
    """Raised when configuration is invalid or missing."""


class DataSourceError(UOADetectorError):
    """Raised when an upstream flow data source fails or returns malformed data."""


class ScoreOutOfRangeError(UOADetectorError):
    """Raised when a sub-score falls outside the documented [0.0, 1.0] range."""

    def __init__(self, name: str, value: float) -> None:
        self.name = name
        self.value = value
        super().__init__(f"Sub-score {name!r} out of range [0.0, 1.0]: got {value}")


class MissingSubScoreError(UOADetectorError):
    """Raised when scoring is attempted before all required sub-scores are populated."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Sub-score {name!r} is None; pipeline did not populate it")


class PipelineStageError(UOADetectorError):
    """Raised when a pipeline stage fails or violates its contract."""


class LabelDecisionError(UOADetectorError):
    """Raised when the labeler cannot reach a coherent decision."""
