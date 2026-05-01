"""Decision-record observability layer.

Builds ``SignalDecisionRecord`` per event and writes them out via one of
three writers (NDJSON / Pretty / Parquet). The pipeline orchestrator
calls ``build_decision_record(...)`` at the end of ``process_one`` and
hands the result to a configured writer.
"""

from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    StageExecutionEntry,
    build_decision_record,
)
from uoa_detector.observability.output import (
    DecisionRecordWriter,
    NDJSONWriter,
    ParquetWriter,
    PrettyWriter,
)

__all__ = [
    "DecisionRecordWriter",
    "NDJSONWriter",
    "ParquetWriter",
    "PrettyWriter",
    "SignalDecisionRecord",
    "StageExecutionEntry",
    "build_decision_record",
]
