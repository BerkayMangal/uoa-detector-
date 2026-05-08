"""Post-event validators (Phase 3.4.8+).

Validators run AFTER signal generation, typically as scheduled
batch jobs. Distinguished from PipelineStages by their lifecycle:
stages run inline at event time; validators run on completed
signal sets at T+1 or later.

The first validator is M28 (next-day OI confirmation).
"""

from uoa_detector.pipeline.validators.m28_next_day_oi import (
    M28Validator,
    ValidationStats,
)

__all__ = ["M28Validator", "ValidationStats"]
