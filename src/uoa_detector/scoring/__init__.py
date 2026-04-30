"""Scoring engines: combined score, early convexity score, penalties."""

from uoa_detector.scoring.combined import (
    compute_combined_score,
    compute_early_convexity_score,
)
from uoa_detector.scoring.early import early_convexity_score
from uoa_detector.scoring.penalties import PenaltyEngine

__all__ = [
    "PenaltyEngine",
    "compute_combined_score",
    "compute_early_convexity_score",
    "early_convexity_score",
]
