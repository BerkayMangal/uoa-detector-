"""Domain models — pure data, no behavior beyond validation."""

from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.domain.scores import CombinedScore, SubScores

__all__ = [
    "AppliedPenalty",
    "CombinedScore",
    "EnrichedEvent",
    "LabelDecision",
    "OptionsPrint",
    "PositionSize",
    "RiskBucket",
    "SignalLabel",
    "SubScores",
]
