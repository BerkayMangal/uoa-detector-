"""Decision-record observability layer.

Builds ``SignalDecisionRecord`` per event and writes them out via one of
three writers (NDJSON / Pretty / Parquet). The pipeline orchestrator
calls ``build_decision_record(...)`` at the end of ``process_one`` and
hands the result to a configured writer.

Phase 3.3.1.2: also ships ``redact_secrets`` — a structlog processor
that scrubs credential-named keys before the renderer sees them.
"""

from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    StageExecutionEntry,
    build_decision_record,
)
from uoa_detector.observability.diagnostics import (
    FlowStats,
    ModuleHealth,
    format_enrichment_line,
    format_flow_line,
    module_health,
)
from uoa_detector.observability.digest import (
    DigestRow,
    render_markdown,
    render_stdout,
    screen_records,
    screen_top_n,
)
from uoa_detector.observability.output import (
    CollectingWriter,
    DecisionRecordWriter,
    NDJSONWriter,
    ParquetWriter,
    PrettyWriter,
)
from uoa_detector.observability.redact import redact_secrets

__all__ = [
    "CollectingWriter",
    "DecisionRecordWriter",
    "DigestRow",
    "FlowStats",
    "ModuleHealth",
    "NDJSONWriter",
    "ParquetWriter",
    "PrettyWriter",
    "SignalDecisionRecord",
    "StageExecutionEntry",
    "build_decision_record",
    "format_enrichment_line",
    "format_flow_line",
    "module_health",
    "redact_secrets",
    "render_markdown",
    "render_stdout",
    "screen_records",
    "screen_top_n",
]
